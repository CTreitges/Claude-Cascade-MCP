"""Self-contained helpers used by `cascade.core.run_cascade`.

These were inline blocks inside the orchestrator until they got long
enough to drown the actual loop logic. Each helper here:

  - takes only its dependencies as arguments (no closures over outer state)
  - never raises (best-effort gathering — never block the run)
  - returns a single value the orchestrator splices into its prompt-context

Behaviour is identical to the inline versions; this is purely a
readability split. The orchestrator now reads top-down without the
~200 lines of context-gathering noise upfront.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .config import Settings
from .memory import recall_context

log = logging.getLogger("cascade")


async def gather_external_research(
    task: str,
    *,
    s: Settings,
    on_log,
) -> str | None:
    """Fetch Context7 lib docs + Brave web hits for `task`. Best-effort —
    returns None on disabled flags or errors.

    `on_log(level, message)` is the orchestrator's log emitter — async
    callable, takes ("info"|"warn", str). Decoupling lets us avoid
    importing Store / task_id here.
    """
    if not (s.cascade_context7_enabled or s.cascade_websearch_enabled):
        return None
    try:
        from .research import gather_external_context
        ext = await gather_external_context(
            task,
            enabled_context7=s.cascade_context7_enabled,
            enabled_websearch=s.cascade_websearch_enabled,
        )
        if ext:
            await on_log("info", f"external_context: {len(ext)} chars fetched")
        return ext
    except Exception as e:
        await on_log("warn", f"external research failed: {e}")
        return None


async def append_repo_style_hints(
    base: str | None,
    *,
    repo: Path | None,
    lang: str,
    on_log,
) -> str | None:
    """If `repo` is a local path, sniff pyproject/.ruff.toml/.editorconfig
    and append a markdown block to `base`. Returns the (possibly extended)
    base; passes through unchanged when there's no repo or no hints."""
    if repo is None:
        return base
    try:
        from .style_probe import format_style_hints, probe_repo_style
        hints = await asyncio.to_thread(probe_repo_style, repo)
        block = format_style_hints(hints, lang=lang)
        if not block:
            return base
        await on_log("info", f"repo-style hints: {sorted(hints.keys())}")
        return f"{base}\n\n{block}" if base else block
    except Exception as e:
        await on_log("warn", f"repo-style probe failed: {e}")
        return base


async def discover_repo_candidates_for_planner(
    task: str,
    *,
    repo: Path | None,
    on_log,
) -> str | None:
    """When the caller didn't pin a repo, discover local repos and render
    a planner-prompt block listing them. Returns None when repo is given
    or discovery yielded nothing."""
    if repo is not None:
        return None
    try:
        from .repo_resolver import discover_local_repos, repos_for_planner_prompt
        local_repos = await asyncio.to_thread(discover_local_repos)
        if not local_repos:
            return None
        block = repos_for_planner_prompt(local_repos, task)
        await on_log("info", f"discovered {len(local_repos)} local repos for planner")
        return block
    except Exception as e:
        await on_log("warn", f"repo discovery failed: {e}")
        return None


async def fetch_recall_context(task: str, *, on_log) -> str | None:
    """Wrap `memory.recall_context` with the orchestrator's logging.
    Returns the recall string (already trimmed) or None."""
    try:
        recall = await recall_context(task)
    except Exception as e:
        await on_log("warn", f"recall_context failed: {e}")
        return None
    if recall:
        await on_log("info", f"recall: {recall[:200]}…")
    return recall
