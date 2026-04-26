"""Per-task workspace: sandboxed file ops + git diff for the reviewer."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class FileOp(BaseModel):
    op: Literal["write", "edit", "delete"]
    path: str
    content: str | None = None
    # For "edit": replace `find` with `replace` exactly once. If find is None, behaves like "write".
    find: str | None = None
    replace: str | None = None

    @field_validator("path")
    @classmethod
    def _no_abs(cls, v: str) -> str:
        if not v or v.startswith("/") or v.startswith("\\"):
            raise ValueError("path must be relative and non-empty")
        return v


def _find_function_stubs(tree) -> list[str]:
    """Return the names of functions/methods whose body is JUST a stub —
    i.e. `pass` (single statement), `...` (Ellipsis-as-stmt) or
    `raise NotImplementedError(...)`. Used by Workspace._validate_content
    to refuse writing half-baked code. A function with a docstring +
    `pass` IS still a stub for our purposes — implementer should put a
    real body."""
    import ast as _ast
    out: list[str] = []
    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        body = list(node.body)
        # Skip a single docstring at the top (still considered a stub if
        # nothing else follows it).
        if (
            body
            and isinstance(body[0], _ast.Expr)
            and isinstance(body[0].value, _ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        if len(body) != 1:
            continue
        stmt = body[0]
        is_stub = False
        if isinstance(stmt, _ast.Pass):
            is_stub = True
        elif (
            isinstance(stmt, _ast.Expr)
            and isinstance(stmt.value, _ast.Constant)
            and stmt.value.value is Ellipsis
        ):
            is_stub = True
        elif isinstance(stmt, _ast.Raise):
            exc = stmt.exc
            if exc is None:
                pass  # bare `raise` is for re-raise inside except — not a stub
            elif (
                isinstance(exc, _ast.Name) and exc.id == "NotImplementedError"
            ):
                is_stub = True
            elif (
                isinstance(exc, _ast.Call)
                and isinstance(exc.func, _ast.Name)
                and exc.func.id == "NotImplementedError"
            ):
                is_stub = True
        if is_stub:
            out.append(node.name)
    return out


@dataclass
class OpResult:
    op: str
    path: str
    ok: bool
    detail: str = ""


class WorkspaceError(Exception):
    pass


@dataclass
class CheckResult:
    name: str
    ok: bool
    exit_code: int
    output: str
    duration_s: float


class WorkspaceLockError(WorkspaceError):
    """Raised when another live cascade run already holds the workspace lock."""


class Workspace:
    """A sandboxed working directory backed by git for diffs."""

    LOCK_FILENAME = ".cascade-lock"

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        if not self.root.exists():
            raise WorkspaceError(f"Workspace does not exist: {self.root}")
        # When True we're working in an existing user repo; we MUST NOT create
        # iter-N commits there (it pollutes their history). Diffs use base_ref.
        self.is_attached: bool = False
        self.base_ref: str | None = None
        self._lock_pid: int | None = None

    # ---------- locking ----------

    def _lock_path(self) -> Path:
        return self.root / self.LOCK_FILENAME

    def _pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except Exception:
            return False

    def acquire_lock(self) -> None:
        """Refuse to start if another live cascade pid is already running
        in this workspace. Stale locks (dead pid) are reaped automatically.
        """
        lock = self._lock_path()
        if lock.exists():
            try:
                content = lock.read_text(encoding="utf-8").strip()
                pid = int(content.split()[0]) if content else 0
            except Exception:
                pid = 0
            if pid and self._pid_alive(pid) and pid != os.getpid():
                raise WorkspaceLockError(
                    f"workspace {self.root} is locked by live pid {pid} — "
                    f"another cascade is already running here. Cancel that "
                    f"task first or wait for it to finish."
                )
            # stale → silently reclaim
        try:
            lock.write_text(f"{os.getpid()}\n{time.time():.0f}", encoding="utf-8")
            self._lock_pid = os.getpid()
        except Exception:  # never let lock-management break the run itself
            pass

    def release_lock(self) -> None:
        if self._lock_pid is None:
            return
        try:
            self._lock_path().unlink(missing_ok=True)
        except Exception:
            pass
        self._lock_pid = None

    # ---------- factory / lifecycle ----------

    @classmethod
    def create(cls, base_dir: Path, task_id: str | None = None) -> "Workspace":
        """Create or re-attach to a workspace directory.

        Idempotent: if `base_dir/task_id` already exists (e.g. from a resumed
        run, a retried spawn, or a previous crash), we re-use it instead of
        crashing with FileExistsError. Git init / initial commit are skipped
        when `.git` is already present.
        """
        base_dir = Path(base_dir)
        base_dir.mkdir(parents=True, exist_ok=True)
        tid = task_id or uuid.uuid4().hex[:12]
        root = (base_dir / tid).resolve()
        already_existed = root.exists()
        root.mkdir(parents=True, exist_ok=True)
        ws = cls(root)
        if not (root / ".git").exists():
            ws._git(["init", "-q"])
            ws._git(["config", "user.email", "cascade@local"])
            ws._git(["config", "user.name", "Cascade"])
            ws._git(["commit", "--allow-empty", "-q", "-m", "init"])
        elif not already_existed:
            # extreme edge: dir was racey-created by another path. fall through.
            pass
        return ws

    @classmethod
    def attach(cls, repo_path: Path) -> "Workspace":
        """Attach to an existing repo (used when --repo is set or planner picks one).

        Records the current HEAD as `base_ref` so subsequent diffs only show what
        Cascade itself changed — and crucially, suppresses iter-N commits that
        would otherwise pollute the user's history.

        For non-git directories: bootstraps a local `.git` so changed_paths()
        and the reviewer-diff still work. The user's existing files become
        the base_ref so we still only report what Cascade itself wrote.
        """
        ws = cls(Path(repo_path))
        ws.is_attached = True
        if (ws.root / ".git").exists():
            r = ws._git(["rev-parse", "HEAD"])
            if r.returncode == 0:
                ws.base_ref = r.stdout.strip() or None
            return ws
        # Bootstrap a local-only git so diffs still work in attached mode.
        ws._git(["init", "-q"])
        ws._git(["config", "user.email", "cascade@local"])
        ws._git(["config", "user.name", "cascade"])
        # Common noise that shouldn't show up in changed_files reporting.
        gitignore = ws.root / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(
                "__pycache__/\n*.pyc\n*.pyo\n.pytest_cache/\n.venv/\nvenv/\n"
                "node_modules/\n.DS_Store\n"
            )
        ws._git(["add", "-A"])
        # Empty dir is fine — commit anyway with --allow-empty so HEAD exists.
        ws._git(["commit", "-q", "--allow-empty", "-m", "cascade base"])
        r = ws._git(["rev-parse", "HEAD"])
        if r.returncode == 0:
            ws.base_ref = r.stdout.strip() or None
        return ws

    def cleanup(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)

    # ---------- safety ----------

    def _safe_path(self, rel: str) -> Path:
        candidate = (self.root / rel).resolve()
        if candidate != self.root and not candidate.is_relative_to(self.root):
            raise WorkspaceError(f"Path escape attempt: {rel!r} → {candidate}")
        return candidate

    # ---------- file ops ----------

    # Generated artifacts the implementer should NEVER touch — writing to
    # them is almost always a sign that the model targeted the compiled
    # bytecode instead of the source file. Caused a real prod-loop where
    # the implementer wrote .pyc files and the reviewer kept saying
    # "source not modified".
    _GENERATED_PATH_RX = re.compile(
        r"(^|/)(__pycache__/|\.pyc$|\.pyo$|\.so$|\.dylib$|node_modules/|"
        r"\.next/|dist/|build/|target/|\.venv/|venv/)"
    )

    @staticmethod
    def _validate_content(path: str, content: str) -> None:
        """Pre-write quality gate. Raises WorkspaceError when the content
        is obviously broken so the implementer gets a precise reviewer-
        style hint on the next iteration instead of a confusing crash
        deep in pytest / py_compile / etc.

        Checks:
          - .py: ast.parse() — catches missing colons, unbalanced parens,
            etc. before they hit the workspace.
          - .json: json.loads()
          - .yaml/.yml: yaml.safe_load() if PyYAML is available
          - .toml: tomllib.loads() (Python 3.11+)
          - Stub-detection on .py: function bodies that consist only of
            `raise NotImplementedError`, bare `pass`, or `...` are flagged
            ONLY if they're newly introduced by this op (we re-read the
            file we're about to overwrite to compare).

        The stub check is conservative: a `pass` with a comment like
        `pass  # placeholder` IS allowed, because the comment may carry
        intent. Only true stubs caught by AST count.
        """
        if not content:
            return
        # Lightweight extension-based dispatch.
        lower = path.lower()
        try:
            if lower.endswith(".py"):
                import ast as _ast
                try:
                    tree = _ast.parse(content)
                except SyntaxError as e:
                    raise WorkspaceError(
                        f"python syntax error in {path}: line {e.lineno}, "
                        f"col {e.offset}: {e.msg}"
                    ) from e
                # Stub-detection
                stubs = _find_function_stubs(tree)
                if stubs:
                    names = ", ".join(stubs[:5])
                    raise WorkspaceError(
                        f"refusing to write {path}: function(s) {names} "
                        f"are stubs (raise NotImplementedError / bare pass / "
                        f"`...`). Implement the body or remove the function."
                    )
            elif lower.endswith(".json"):
                import json as _json
                try:
                    _json.loads(content)
                except _json.JSONDecodeError as e:
                    raise WorkspaceError(
                        f"json parse error in {path}: line {e.lineno}, "
                        f"col {e.colno}: {e.msg}"
                    ) from e
            elif lower.endswith((".yaml", ".yml")):
                try:
                    import yaml as _yaml  # type: ignore[import-untyped]
                except ImportError:
                    return
                try:
                    _yaml.safe_load(content)
                except _yaml.YAMLError as e:
                    raise WorkspaceError(
                        f"yaml parse error in {path}: {e}"
                    ) from e
            elif lower.endswith(".toml"):
                try:
                    import tomllib as _toml  # py311+
                except ImportError:
                    return
                try:
                    _toml.loads(content)
                except _toml.TOMLDecodeError as e:
                    raise WorkspaceError(
                        f"toml parse error in {path}: {e}"
                    ) from e
        except WorkspaceError:
            raise
        except Exception:
            # Never let validation crash apply_ops on something exotic
            # (e.g. content with unusual encoding) — fail open for
            # unrecognised cases.
            return

    def apply_ops(self, ops: list[FileOp | dict]) -> list[OpResult]:
        results: list[OpResult] = []
        for raw in ops:
            op = raw if isinstance(raw, FileOp) else FileOp.model_validate(raw)
            try:
                # Refuse generated/compiled artifacts up-front — writing to
                # them looks like progress to the implementer but the source
                # file stays untouched and the next iteration loops forever.
                if op.path and self._GENERATED_PATH_RX.search(op.path):
                    raise WorkspaceError(
                        f"refusing op on generated artifact path: {op.path!r}. "
                        f"Edit the SOURCE file instead (e.g. drop the .pyc / "
                        f"__pycache__ prefix)."
                    )
                if op.op == "write":
                    if op.content is None:
                        raise WorkspaceError("write requires `content`")
                    self._validate_content(op.path, op.content)
                    p = self._safe_path(op.path)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(op.content, encoding="utf-8")
                    results.append(OpResult("write", op.path, True, f"{len(op.content)}B"))

                elif op.op == "edit":
                    p = self._safe_path(op.path)
                    if not p.exists():
                        raise WorkspaceError(f"edit target missing: {op.path}")
                    if op.find is None or op.replace is None:
                        # Fallback: full overwrite via `content`.
                        if op.content is None:
                            raise WorkspaceError("edit requires either find+replace or content")
                        self._validate_content(op.path, op.content)
                        p.write_text(op.content, encoding="utf-8")
                        results.append(OpResult("edit", op.path, True, "overwrite"))
                    else:
                        text = p.read_text(encoding="utf-8")
                        count = text.count(op.find)
                        if count == 0:
                            raise WorkspaceError(f"find string not found in {op.path}")
                        if count > 1:
                            raise WorkspaceError(
                                f"find string is not unique in {op.path} ({count} matches)"
                            )
                        new_text = text.replace(op.find, op.replace, 1)
                        self._validate_content(op.path, new_text)
                        p.write_text(new_text, encoding="utf-8")
                        results.append(OpResult("edit", op.path, True, "1 replacement"))

                elif op.op == "delete":
                    p = self._safe_path(op.path)
                    if p.is_dir():
                        shutil.rmtree(p)
                    elif p.exists():
                        p.unlink()
                    else:
                        raise WorkspaceError(f"delete target missing: {op.path}")
                    results.append(OpResult("delete", op.path, True, ""))

                else:  # pragma: no cover — pydantic guards this
                    raise WorkspaceError(f"unknown op: {op.op}")

            except Exception as e:
                results.append(OpResult(op.op, op.path, False, str(e)))
        return results

    # ---------- git helpers ----------

    def _git(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=False,
            capture_output=True,
            text=True,
        )

    def stage_all(self) -> None:
        self._git(["add", "-A"])

    def diff(self, max_bytes: int = 200_000) -> str:
        """Return diff describing Cascade's most recent iteration's work.

        - Detached: stage everything → diff vs. last `iter` commit (i.e. only
          the new changes since the previous commit_iteration).
        - Attached: diff working-tree vs. base_ref recorded at attach time.

        Each per-iter Reviewer call uses this to focus on the latest delta.
        For the cross-subtask Integration Review use `diff_cumulative()`.
        """
        if self.is_attached and self.base_ref:
            self.stage_all()
            out = self._git(["diff", "--cached", self.base_ref]).stdout
        else:
            self.stage_all()
            out = self._git(["diff", "--cached"]).stdout
        if len(out) > max_bytes:
            out = out[:max_bytes] + f"\n…(truncated, {len(out) - max_bytes} more bytes)"
        return out

    def diff_cumulative(self, max_bytes: int = 200_000) -> str:
        """Cumulative diff since the run started.

        - Detached: from the initial empty commit through every committed
          iter-commit + any uncommitted staged work.
        - Attached: from `base_ref` to current.

        Used by the supervisor's final Integration-Review so it sees every
        file the run produced, not just the last sub-task's delta. (Pre-fix
        the per-iter diff() was empty after commit_iteration → integration
        reviewer hallucinated `empty diff` and rejected fully working runs.)
        """
        # Pick comparison base.
        base = self.base_ref if (self.is_attached and self.base_ref) else None
        if base is None:
            r = self._git(["rev-list", "--max-parents=0", "HEAD"])
            if r.returncode == 0 and r.stdout.strip():
                base = r.stdout.strip().splitlines()[0]
        self.stage_all()
        out = ""
        if base:
            r = self._git(["diff", base])
            if r.returncode == 0:
                out = r.stdout
        if not out:
            r = self._git(["diff", "--cached"])
            out = r.stdout if r.returncode == 0 else ""
        if len(out) > max_bytes:
            out = out[:max_bytes] + f"\n…(truncated, {len(out) - max_bytes} more bytes)"
        return out

    def commit_iteration(self, n: int) -> None:
        """Commit current changes as iter-N. No-op on attached user repos —
        we MUST NOT pollute their history with internal iteration markers."""
        if self.is_attached:
            return
        self.stage_all()
        self._git(["commit", "-q", "--allow-empty", "-m", f"iter {n}"])

    # ---------- quality checks ----------

    async def run_check(self, check: "QualityCheck") -> CheckResult:
        """Run a planner-defined quality check inside the workspace.

        Streams stdout+stderr together, kills on timeout, caps output to ~16kB.
        """
        started = asyncio.get_event_loop().time()
        env = {**os.environ}
        try:
            proc = await asyncio.create_subprocess_shell(
                check.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.root),
                env=env,
            )
        except Exception as e:
            return CheckResult(check.name, False, -1, f"spawn-error: {e}", 0.0)

        try:
            out_b, _ = await asyncio.wait_for(
                proc.communicate(), timeout=max(1, check.timeout_s)
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            duration = asyncio.get_event_loop().time() - started
            return CheckResult(
                check.name, False, -2, f"timeout after {check.timeout_s}s", duration
            )

        duration = asyncio.get_event_loop().time() - started
        out = out_b.decode("utf-8", errors="replace")
        if len(out) > 16_000:
            out = out[:16_000] + f"\n…(truncated, +{len(out) - 16_000} chars)"

        ok = proc.returncode == 0 if check.must_succeed else True
        if check.expected_substring and check.expected_substring not in out:
            ok = False
        return CheckResult(check.name, ok, proc.returncode or 0, out, duration)


    def list_files(self) -> list[str]:
        out = self._git(["ls-files"]).stdout
        return [line for line in out.splitlines() if line]

    def changed_paths(self) -> list[str]:
        """Return only the paths Cascade actually touched this run.

        - Attached repo: diff vs `base_ref` (HEAD-at-attach-time). This excludes
          everything the user already had — venvs, caches, pre-existing files.
        - Detached/fresh workspace: diff vs HEAD (the initial 'init' commit).
        """
        if self.is_attached and self.base_ref:
            self.stage_all()
            out = self._git(["diff", "--cached", "--name-only", self.base_ref]).stdout
        else:
            self.stage_all()
            out = self._git(["diff", "--cached", "--name-only", "HEAD"]).stdout
        return [line for line in out.splitlines() if line]

    def read_files(
        self,
        paths: list[str],
        *,
        max_bytes: int = 60_000,
        max_per_file: int = 20_000,
    ) -> dict[str, str]:
        """Read a curated set of workspace files into memory, with sandboxing
        and a total-byte budget. Truncates per-file and bails out when the
        global budget is exhausted so the implementer prompt stays bounded.
        """
        out: dict[str, str] = {}
        used = 0
        for rel in paths:
            if used >= max_bytes:
                break
            try:
                p = self._safe_path(rel)
            except WorkspaceError:
                continue
            if not p.is_file():
                continue
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            # per-file truncation
            if len(content) > max_per_file:
                content = content[:max_per_file] + f"\n…(truncated, +{len(content) - max_per_file} chars)"
            # global budget
            if used + len(content) > max_bytes:
                budget_left = max_bytes - used
                content = content[:budget_left] + "\n…(truncated, global budget exhausted)"
            out[rel] = content
            used += len(content)
        return out

    def candidate_context_files(self, hints: list[str], limit: int = 12) -> list[str]:
        """Pick the most relevant files for an implementer call.

        Strategy:
        1. Files explicitly named in the plan that exist in the workspace.
        2. Plain-text files in the workspace whose name matches any hint
           (basename match), to catch typos like "config" vs "config.py".
        3. Top-level Python/markdown files as a generic fallback.

        Caps at `limit` so the prompt stays bounded.
        """
        all_files = set(self.list_files())
        picked: list[str] = []
        seen: set[str] = set()

        def take(p: str) -> None:
            if p in seen or p not in all_files:
                return
            seen.add(p)
            picked.append(p)

        # 1) exact hits from the plan
        for h in hints:
            take(h)
            if len(picked) >= limit:
                return picked

        # 2) basename matches
        if len(picked) < limit:
            hint_bases = {h.split("/")[-1].lower() for h in hints}
            for f in sorted(all_files):
                if f.split("/")[-1].lower() in hint_bases:
                    take(f)
                    if len(picked) >= limit:
                        return picked

        return picked


class QualityCheck(BaseModel):
    """A planner-declared, objectively-verifiable check executed in the workspace.

    Defined here (not in agents/planner.py) so workspace.run_check can take it
    as a parameter without a circular import. Re-exported from agents.planner.
    """
    name: str = Field(..., description="Short human-readable name, e.g. 'pytest'.")
    command: str = Field(..., description="Shell command, run with cwd=workspace root.")
    must_succeed: bool = True
    expected_substring: str | None = None
    timeout_s: int = 60


# ---------- async maintenance ----------


async def cleanup_old_workspaces(base_dir: Path, retention_days: int) -> int:
    """Delete workspace dirs older than retention_days. Returns count deleted."""

    def _do() -> int:
        if not base_dir.exists():
            return 0
        cutoff = time.time() - retention_days * 86400
        removed = 0
        for child in base_dir.iterdir():
            if not child.is_dir():
                continue
            try:
                mtime = child.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        return removed

    return await asyncio.to_thread(_do)
