"""Discover local repos and resolve a Plan's repo intent into a usable path.

Strategy:
  - At plan time we hand the planner a short list of locally-known git repos.
  - The planner can return a `repo` hint with kind ∈ {local, clone, fresh}.
  - resolve_repo() turns that hint into either an existing path (Workspace.attach)
    or a fresh tmp workspace (Workspace.create).
  - If kind=local but the path is missing AND a clone URL is provided, we clone.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("cascade.repo_resolver")


@dataclass
class ResolvedRepo:
    path: Path | None        # None → caller should create a fresh tmp workspace
    attached: bool           # True → existing repo (don't init/cleanup)
    source: str              # "local" | "cloned" | "fresh" | "fallback"
    note: str = ""


def discover_local_repos(
    extra_roots: list[Path] | None = None,
    *,
    max_depth: int = 2,
    limit: int = 30,
) -> list[Path]:
    """Walk a few well-known roots looking for `.git/` directories.

    Roots checked: $HOME/projekte/, $HOME/repos/, $HOME/code/, $HOME/dev/,
    $HOME/, plus anything in `extra_roots`. Depth-limited so we don't dive
    into node_modules / venv hierarchies.
    """
    home = Path.home()
    roots = [
        home / "projekte",
        home / "repos",
        home / "code",
        home / "dev",
        home,
    ]
    if extra_roots:
        roots.extend(extra_roots)

    seen: set[Path] = set()
    found: list[Path] = []

    def _scan(root: Path, depth: int) -> None:
        if depth > max_depth or len(found) >= limit:
            return
        if not root.is_dir():
            return
        try:
            entries = list(root.iterdir())
        except (PermissionError, OSError):
            return
        # If this dir is itself a repo, record it but still descend one level
        # (some users keep nested clones). Skip dot-dirs (.cache, .config) as roots.
        if (root / ".git").is_dir() and root not in seen:
            seen.add(root)
            found.append(root)
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if entry.name in {"node_modules", "venv", ".venv", "__pycache__", "build", "dist"}:
                continue
            _scan(entry, depth + 1)

    for r in roots:
        _scan(r, 0)
    return found[:limit]


def repos_for_planner_prompt(repos: list[Path], task: str) -> str:
    """Render a short text block for the planner system prompt."""
    if not repos:
        return ""
    lines = ["Locally available git repos (the planner may pick one if the task references it):"]
    for r in repos:
        lines.append(f"- {r}")
    return "\n".join(lines)


async def _git_clone(url: str, target: Path, *, timeout_s: float = 180) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth", "1", url, str(target),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"git clone {url} timed out after {timeout_s}s")
    if proc.returncode != 0:
        out = out_b.decode("utf-8", errors="replace")
        raise RuntimeError(f"git clone {url} failed (rc={proc.returncode}): {out[:600]}")


async def resolve_repo(
    repo_hint,                    # cascade.agents.planner.RepoHint | None
    *,
    workspaces_root: Path,
    task_id: str,
) -> ResolvedRepo:
    """Apply the planner's repo hint. Returns ResolvedRepo with .path and .attached.

    Caller passes the workspaces_root (where to put a clone if needed) and the
    task_id (so the clone target is unique per task).
    """
    if repo_hint is None:
        return ResolvedRepo(path=None, attached=False, source="fresh", note="no hint")

    kind = getattr(repo_hint, "kind", None) or "fresh"
    path = getattr(repo_hint, "path", None) or None
    url = getattr(repo_hint, "url", None) or None

    if kind == "local" and path:
        p = Path(path).expanduser().resolve()
        if p.is_dir():
            return ResolvedRepo(path=p, attached=True, source="local", note=f"reusing {p}")
        # Fallback: try cloning if URL is also provided.
        if url:
            target = (workspaces_root / f"{task_id}-clone").resolve()
            try:
                await _git_clone(url, target)
            except Exception as e:
                return ResolvedRepo(
                    path=None, attached=False, source="fallback",
                    note=f"local path missing ({p}) and clone failed: {e}"
                )
            return ResolvedRepo(path=target, attached=True, source="cloned",
                                note=f"local missing, cloned {url} → {target}")
        return ResolvedRepo(path=None, attached=False, source="fallback",
                            note=f"local path {p} not found, no clone url")

    if kind == "clone" and url:
        target = (workspaces_root / f"{task_id}-clone").resolve()
        try:
            await _git_clone(url, target)
        except Exception as e:
            return ResolvedRepo(path=None, attached=False, source="fallback",
                                note=f"clone failed: {e}")
        return ResolvedRepo(path=target, attached=True, source="cloned",
                            note=f"cloned {url} → {target}")

    # default: fresh
    return ResolvedRepo(path=None, attached=False, source="fresh", note="planner chose fresh workspace")
