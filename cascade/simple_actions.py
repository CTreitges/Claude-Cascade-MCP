"""Direct-action executor for the chat worker.

When the triage layer decides a request is small enough to skip the full
Plan→Implement→Review cascade (e.g. "drop this credential at ~/.config/...",
"set SCDL_DRIVE_FOLDER_ID in .env to abc"), it returns a `direct_action`
descriptor instead of a `task`. The bot then runs the action through
`run_action()` here, captures a per-action log, and hands it to
`cascade.quick_review.review_action()` for a sanity check before replying
to the user.

Allowed action kinds:
  - write_file:     create/overwrite a file at a path-whitelisted location
  - edit_env:       set/update KEY=VALUE in a .env file
  - place_file:     copy a previously-staged file to a target (chmod 600)
  - read_file:      read & return content (for "show me X")

Path safety: targets must be under one of `_ALLOWED_ROOTS`. Anything else
raises ActionError. The reviewer still gets the full path so it can
double-check.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Targets must live under one of these roots. Keeps direct-actions away
# from /etc, /usr, /var, ssh keys, etc. — anything destructive needs
# the full cascade with planner+reviewer.
_ALLOWED_ROOTS: tuple[Path, ...] = (
    Path.home() / ".config",
    Path.home() / "claude-cascade",
    Path.home() / "projekte",
    Path.home() / "Projects",
    Path("/tmp"),
)


class ActionError(Exception):
    pass


@dataclass
class ActionResult:
    kind: str
    summary: str
    ok: bool
    log: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)
    output: str = ""
    error: str | None = None


def _safe_target(path_str: str) -> Path:
    p = Path(path_str).expanduser().resolve()
    for root in _ALLOWED_ROOTS:
        try:
            p.relative_to(root.resolve())
            return p
        except ValueError:
            continue
    raise ActionError(
        f"path outside allowed roots: {p}. Allowed roots are "
        + ", ".join(str(r) for r in _ALLOWED_ROOTS)
        + ". Use a full cascade for paths outside these."
    )


def _write_file(params: dict[str, Any]) -> ActionResult:
    target = _safe_target(params["target"])
    content: str = params.get("content", "")
    mode = int(params.get("mode", 0o644))
    target.parent.mkdir(parents=True, exist_ok=True)
    pre_existed = target.exists()
    target.write_text(content, encoding="utf-8")
    target.chmod(mode)
    return ActionResult(
        kind="write_file",
        summary=f"{'overwrote' if pre_existed else 'created'} {target} ({len(content)}B, mode={oct(mode)})",
        ok=True,
        log=[f"write {target} {len(content)}B mode={oct(mode)}"],
        files_touched=[str(target)],
    )


def _place_file(params: dict[str, Any]) -> ActionResult:
    source = Path(params["source"]).expanduser()
    target = _safe_target(params["target"])
    mode = int(params.get("mode", 0o600))
    if not source.is_file():
        raise ActionError(f"source file does not exist: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.parent.chmod(0o700)
    except Exception:
        pass
    shutil.copyfile(source, target)
    target.chmod(mode)
    return ActionResult(
        kind="place_file",
        summary=f"copied {source} → {target} (mode={oct(mode)})",
        ok=True,
        log=[f"copy {source} → {target}", f"chmod {oct(mode)}"],
        files_touched=[str(target)],
    )


def _edit_env(params: dict[str, Any]) -> ActionResult:
    """Set KEY=VALUE in a .env file. Updates an existing line if present,
    appends otherwise. The .env file must live under one of the allowed
    roots and must end in `.env` or contain `.env.` in its name."""
    target = _safe_target(params["target"])
    if target.suffix != ".env" and ".env" not in target.name:
        raise ActionError(f"edit_env target must be a .env file: {target}")
    key = str(params["key"])
    value = str(params.get("value", ""))
    if not re.match(r"^[A-Z][A-Z0-9_]*$", key):
        raise ActionError(f"invalid env key: {key!r} (must be UPPERCASE_SNAKE)")
    line = f"{key}={value}\n"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(line, encoding="utf-8")
        return ActionResult(
            kind="edit_env",
            summary=f"created {target} with {key}=...",
            ok=True,
            log=[f"create {target}", f"set {key}"],
            files_touched=[str(target)],
        )
    text = target.read_text(encoding="utf-8")
    pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
    if pattern.search(text):
        new_text = pattern.sub(line.rstrip("\n"), text)
        action = "update"
    else:
        if not text.endswith("\n"):
            text += "\n"
        new_text = text + line
        action = "append"
    target.write_text(new_text, encoding="utf-8")
    return ActionResult(
        kind="edit_env",
        summary=f"{action} {key} in {target}",
        ok=True,
        log=[f"{action} {key} in {target}"],
        files_touched=[str(target)],
    )


def _read_file(params: dict[str, Any]) -> ActionResult:
    target = _safe_target(params["target"])
    if not target.is_file():
        raise ActionError(f"file does not exist: {target}")
    if target.stat().st_size > 200_000:
        raise ActionError(f"file too large for read_file action: {target} ({target.stat().st_size}B)")
    content = target.read_text(encoding="utf-8")
    return ActionResult(
        kind="read_file",
        summary=f"read {target} ({len(content)}B)",
        ok=True,
        log=[f"read {target}"],
        files_touched=[],
        output=content[:50_000],
    )


_HANDLERS = {
    "write_file": _write_file,
    "place_file": _place_file,
    "edit_env":   _edit_env,
    "read_file":  _read_file,
}


def is_known_kind(kind: str) -> bool:
    return kind in _HANDLERS


def is_target_in_allowlist(path_str: str) -> bool:
    """Public wrapper around `_safe_target`: True when the path resolves
    inside one of `_ALLOWED_ROOTS` (and therefore would be accepted by a
    direct-action). Used by the triage layer to validate proposals BEFORE
    returning them to the bot."""
    try:
        _safe_target(path_str)
        return True
    except ActionError:
        return False
    except Exception:
        return False


async def run_action(action: dict[str, Any]) -> ActionResult:
    """Execute a direct-action descriptor. Catches all errors and returns
    an ActionResult with ok=False rather than raising."""
    kind = action.get("kind")
    if not isinstance(kind, str) or kind not in _HANDLERS:
        return ActionResult(
            kind=str(kind), summary=f"unknown action kind: {kind!r}",
            ok=False, error="unknown kind",
        )
    try:
        params = action.get("params") or {}
        return _HANDLERS[kind](params)
    except ActionError as e:
        return ActionResult(
            kind=kind, summary=f"action rejected: {e}",
            ok=False, error=str(e),
        )
    except Exception as e:
        return ActionResult(
            kind=kind, summary=f"action crashed: {type(e).__name__}: {e}",
            ok=False, error=f"{type(e).__name__}: {e}",
        )
