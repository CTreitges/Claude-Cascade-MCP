"""RLM-Claude bridge for cross-task memory.

This is best-effort: if the rlm-claude MCP isn't reachable from the cascade
process (which is the common case — MCPs are usually scoped to the parent
Claude Code session), we silently no-op rather than crashing the loop.

Two integration paths are supported:
  1. **Direct CLI**: invoke `rlm-claude` (or `mcp__rlm-claude__rlm_remember`) as a
     subprocess if a binary is on PATH.
  2. **HTTP fallback**: if `RLM_HTTP_ENDPOINT` is set, POST to it.

For now we just provide async no-op shims with logging — when an integration
path becomes stable, swap the body without touching call sites.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Literal

log = logging.getLogger("cascade.memory")

PROJECT = "claude-cascade"


async def recall_context(task: str, *, limit: int = 3) -> str | None:
    """Return a short string of recalled context, or None if RLM is unavailable."""
    if not _rlm_available():
        return None
    # Placeholder: structured invocation will be added once we settle on the
    # transport. For now, return None so the planner runs unconditioned.
    log.debug("rlm recall placeholder for task=%r limit=%d", task[:80], limit)
    return None


async def remember_finding(
    content: str,
    *,
    category: Literal["finding", "decision", "preference", "fact"] = "finding",
    importance: Literal["low", "medium", "high", "critical"] = "medium",
    tags: str = "claude-cascade",
) -> bool:
    """Persist an insight to RLM. Returns True on success."""
    if not _rlm_available():
        log.debug("RLM unavailable — skipping remember: %s", content[:100])
        return False
    log.info("rlm remember [%s/%s] %s", category, importance, content[:100])
    # TODO: wire to actual rlm-claude transport
    return True


def _rlm_available() -> bool:
    if os.getenv("RLM_HTTP_ENDPOINT"):
        return True
    if shutil.which("rlm-claude"):
        return True
    return False
