"""MCP stdio server exposing Cascade to Claude Code.

Tools:
  run_cascade(task, repo=None, sync=True, timeout_s=600,
              implementer_model=None, implementer_tools=None) → dict
  cascade_status(task_id) → dict
  cascade_logs(task_id, tail=50) → list[str]
  cascade_cancel(task_id) → dict
  cascade_history(limit=10) → list[dict]
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from cascade.config import settings
from cascade.core import run_cascade
from cascade.store import Store

log = logging.getLogger("cascade.mcp")
mcp = FastMCP("claude-cascade")

# Per-process registry of currently-running tasks for cancel support.
_RUNNING: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}


@mcp.tool()
async def run_cascade_tool(
    task: str,
    repo: str | None = None,
    sync: bool = True,
    timeout_s: int = 600,
    implementer_model: str | None = None,
    implementer_provider: Literal["ollama", "openai_compatible"] | None = None,
    implementer_tools: Literal["fileops", "mcp"] | None = None,
) -> dict:
    """Run a Plan→Implement→Review cascade. sync=True blocks until done or timeout."""
    s = settings()
    cancel = asyncio.Event()

    coro = run_cascade(
        task=task,
        source="mcp",
        repo=Path(repo) if repo else None,
        implementer_model=implementer_model,
        implementer_provider=implementer_provider,
        implementer_tools=implementer_tools,
        s=s,
        cancel_event=cancel,
    )

    if sync:
        try:
            result = await asyncio.wait_for(coro, timeout=timeout_s)
        except asyncio.TimeoutError:
            cancel.set()
            return {"status": "timeout", "summary": f"Exceeded {timeout_s}s"}
        return _result_dict(result)

    # async path: kick off and return id immediately
    task_obj = asyncio.create_task(coro)

    # We don't have task_id until the coro runs; bridge via a small wrapper.
    # Easier: create task row eagerly here, then pass resume_task_id.
    store = await Store.open(s.cascade_db_path)
    try:
        tid = await store.create_task(
            source="mcp",
            task_text=task,
            repo_path=repo,
            implementer_model=implementer_model or s.cascade_implementer_model,
            implementer_tools=implementer_tools or s.cascade_implementer_tools,
        )
    finally:
        await store.close()
    # Replace the task with one that uses the pre-created id
    task_obj.cancel()
    cancel = asyncio.Event()
    task_obj = asyncio.create_task(
        run_cascade(
            task=task,
            source="mcp",
            repo=Path(repo) if repo else None,
            implementer_model=implementer_model,
            implementer_provider=implementer_provider,
            implementer_tools=implementer_tools,
            s=s,
            cancel_event=cancel,
            resume_task_id=tid,
        )
    )
    _RUNNING[tid] = (task_obj, cancel)
    task_obj.add_done_callback(lambda _t, k=tid: _RUNNING.pop(k, None))
    return {"task_id": tid, "status": "running", "sync": False}


@mcp.tool()
async def cascade_status(task_id: str) -> dict:
    """Return current status, iteration, and result summary of a task."""
    s = settings()
    store = await Store.open(s.cascade_db_path)
    try:
        t = await store.get_task(task_id)
        if t is None:
            return {"error": "not found", "task_id": task_id}
        return {
            "task_id": t.id,
            "status": t.status,
            "iteration": t.iteration,
            "summary": t.result_summary,
            "workspace": t.workspace_path,
            "created_at": t.created_at,
            "completed_at": t.completed_at,
        }
    finally:
        await store.close()


@mcp.tool()
async def cascade_logs(task_id: str, tail: int = 50) -> list[str]:
    """Return the last `tail` log lines for a task (chronological)."""
    s = settings()
    store = await Store.open(s.cascade_db_path)
    try:
        entries = await store.tail_logs(task_id, n=tail)
        return [f"[{e.level}] {e.message}" for e in entries]
    finally:
        await store.close()


@mcp.tool()
async def cascade_cancel(task_id: str) -> dict:
    """Cancel a running cascade task."""
    if task_id not in _RUNNING:
        return {"task_id": task_id, "cancelled": False, "reason": "not running in this process"}
    _, ev = _RUNNING[task_id]
    ev.set()
    return {"task_id": task_id, "cancelled": True}


@mcp.tool()
async def cascade_history(limit: int = 10) -> list[dict]:
    """Return the last `limit` tasks (most recent first)."""
    s = settings()
    store = await Store.open(s.cascade_db_path)
    try:
        tasks = await store.list_tasks(limit=limit)
        return [
            {
                "task_id": t.id,
                "status": t.status,
                "task": t.task_text[:120],
                "iteration": t.iteration,
                "created_at": t.created_at,
            }
            for t in tasks
        ]
    finally:
        await store.close()


def _result_dict(r) -> dict:
    return {
        "task_id": r.task_id,
        "status": r.status,
        "iterations": r.iterations,
        "summary": r.summary,
        "workspace": str(r.workspace_path),
        "diff_chars": len(r.diff),
        "error": r.error,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    mcp.run()


if __name__ == "__main__":
    main()
