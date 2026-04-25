"""Per-task workspace: sandboxed file ops + git diff for the reviewer."""

from __future__ import annotations

import asyncio
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


@dataclass
class OpResult:
    op: str
    path: str
    ok: bool
    detail: str = ""


class WorkspaceError(Exception):
    pass


class Workspace:
    """A sandboxed working directory backed by git for diffs."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        if not self.root.exists():
            raise WorkspaceError(f"Workspace does not exist: {self.root}")

    # ---------- factory / lifecycle ----------

    @classmethod
    def create(cls, base_dir: Path, task_id: str | None = None) -> "Workspace":
        base_dir = Path(base_dir)
        base_dir.mkdir(parents=True, exist_ok=True)
        tid = task_id or uuid.uuid4().hex[:12]
        root = (base_dir / tid).resolve()
        root.mkdir(parents=True, exist_ok=False)
        ws = cls(root)
        ws._git(["init", "-q"])
        ws._git(["config", "user.email", "cascade@local"])
        ws._git(["config", "user.name", "Cascade"])
        # Empty initial commit so `git diff HEAD` works from the very first iteration.
        ws._git(["commit", "--allow-empty", "-q", "-m", "init"])
        return ws

    @classmethod
    def attach(cls, repo_path: Path) -> "Workspace":
        """Attach to an existing repo (used when --repo is set)."""
        ws = cls(Path(repo_path))
        # Don't re-init or commit — caller's working tree is sacred.
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

    def apply_ops(self, ops: list[FileOp | dict]) -> list[OpResult]:
        results: list[OpResult] = []
        for raw in ops:
            op = raw if isinstance(raw, FileOp) else FileOp.model_validate(raw)
            try:
                if op.op == "write":
                    if op.content is None:
                        raise WorkspaceError("write requires `content`")
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
                        p.write_text(text.replace(op.find, op.replace, 1), encoding="utf-8")
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
        """Return staged diff vs. last commit, truncated."""
        self.stage_all()
        out = self._git(["diff", "--cached"]).stdout
        if len(out) > max_bytes:
            out = out[:max_bytes] + f"\n…(truncated, {len(out) - max_bytes} more bytes)"
        return out

    def commit_iteration(self, n: int) -> None:
        self.stage_all()
        # only commits if there are staged changes
        self._git(["commit", "-q", "--allow-empty", "-m", f"iter {n}"])

    def list_files(self) -> list[str]:
        out = self._git(["ls-files"]).stdout
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
