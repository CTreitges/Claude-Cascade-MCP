"""Centralized error sink — file log + RLM insight on every captured exception.

Use `await log_error(scope, exc, **context)` from any except-block. The
exception always re-raises is the caller's choice; this helper just
records it. Two artifacts per error:

  1. Append a JSON line to `<cascade_home>/store/errors.log` (rotated by
     keeping the last 500 lines).
  2. Save an RLM finding (importance=high) so cross-session debugging can
     recall it.

Both writes are best-effort: if the sink itself fails we log a warning
but never propagate.
"""

from __future__ import annotations

import json
import logging
import time
import traceback
from pathlib import Path
from typing import Any

from .config import settings
from .memory import remember_finding

log = logging.getLogger("cascade.error_log")

_MAX_LINES = 500


def _log_path() -> Path:
    p = settings().cascade_home / "store" / "errors.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _trim_log(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > _MAX_LINES:
            path.write_text("\n".join(lines[-_MAX_LINES:]) + "\n", encoding="utf-8")
    except Exception:
        pass


async def log_error(
    scope: str,
    exc: BaseException,
    **context: Any,
) -> None:
    """Record an exception. `scope` should be a short stable string like
    'triage', 'runner.run_cascade', 'on_text', 'agent_chat.ollama'.
    `context` is small structured info (chat_id, model, task_id, ...)."""
    ts = time.time()
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    entry = {
        "ts": ts,
        "scope": scope,
        "error": f"{type(exc).__name__}: {exc}",
        "traceback": tb[-3000:],
        "context": {k: _safe(v) for k, v in context.items()},
    }
    # 1) file log
    try:
        path = _log_path()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        _trim_log(path)
    except Exception as e:
        log.warning("error_log file write failed: %s", e)

    # 2) RLM insight — short and grep-friendly
    try:
        ctx_short = ", ".join(f"{k}={_safe(v)}" for k, v in list(context.items())[:6])
        body = (
            f"[{scope}] {type(exc).__name__}: {exc}\n"
            f"context: {ctx_short or '—'}\n"
            f"tail:\n{tb[-800:]}"
        )
        await remember_finding(
            body,
            category="finding",
            importance="high",
            tags=f"claude-cascade,error,{scope}",
        )
    except Exception as e:
        log.warning("error_log rlm insight failed: %s", e)


def _safe(v: Any) -> Any:
    try:
        if isinstance(v, (str, int, float, bool, type(None))):
            return v
        return str(v)[:200]
    except Exception:
        return "<unrepr>"


def tail_errors(n: int = 20) -> list[dict]:
    """Read the last N error entries — useful for /errors-style debug commands."""
    path = _log_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(line) for line in lines if line.strip()]
    except Exception:
        return []
