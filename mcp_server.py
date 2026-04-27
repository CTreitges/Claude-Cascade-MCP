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
mcp = FastMCP("cascade-bot-mcp")

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
    planner_model: str | None = None,
    reviewer_model: str | None = None,
    planner_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None,
    reviewer_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None,
    replan_max: int | None = None,
) -> dict:
    """Run a Plan → Implement → Review cascade.

    The planner (Opus by default) decomposes `task` into steps + acceptance
    criteria + quality_checks + a repo decision (use a local clone, clone a
    URL, or fresh tmp). The implementer (Ollama Cloud by default) writes
    file ops; quality_checks run after every iteration. Failures trigger
    auto-replan up to `replan_max` times.

    Args:
      task: free-form natural-language task description.
      repo: pin a working directory; if omitted, the planner picks one
        (typical: a local repo path it discovers, or a clone URL).
      sync: True (default) blocks up to `timeout_s` and returns the final
        result dict. False returns immediately with a task_id for later
        polling via cascade_status / cascade_logs.
      timeout_s: only used when sync=True.
      implementer_model: override the runtime default (kimi-k2.6);
        e.g. "qwen3-coder:480b", "glm-5.1", "minimax-m2.7", "deepseek-v3.2".
      planner_model / reviewer_model: override the Claude models for this run.
      planner_effort / reviewer_effort: claude-cli --effort flag.
      replan_max: how often the planner may rewrite the plan if the loop
        gets stuck (default 2).

    Returns: {task_id, status, iterations, summary, workspace, diff_chars, error}.
    """
    s = settings()

    if sync:
        cancel = asyncio.Event()
        coro = run_cascade(
            task=task,
            source="mcp",
            repo=Path(repo) if repo else None,
            implementer_model=implementer_model,
            implementer_provider=implementer_provider,
            implementer_tools=implementer_tools,
            planner_model=planner_model,
            reviewer_model=reviewer_model,
            planner_effort=planner_effort,
            reviewer_effort=reviewer_effort,
            replan_max=replan_max,
            s=s,
            cancel_event=cancel,
        )
        try:
            result = await asyncio.wait_for(coro, timeout=timeout_s)
        except asyncio.TimeoutError:
            cancel.set()
            return {"status": "timeout", "summary": f"Exceeded {timeout_s}s"}
        return _result_dict(result)

    # Async path: eagerly create the DB row so the caller gets a stable task_id
    # before any work starts. Then launch run_cascade with resume_task_id so it
    # picks up our pre-created row instead of inserting a duplicate.
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

    cancel = asyncio.Event()
    task_obj = asyncio.create_task(
        run_cascade(
            task=task,
            source="mcp",
            repo=Path(repo) if repo else None,
            implementer_model=implementer_model,
            implementer_provider=implementer_provider,
            implementer_tools=implementer_tools,
            planner_model=planner_model,
            reviewer_model=reviewer_model,
            planner_effort=planner_effort,
            reviewer_effort=reviewer_effort,
            replan_max=replan_max,
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
    """Return current status, iteration, and result summary of a cascade task.

    Status is one of: pending / running / interrupted / done / failed / cancelled.
    Includes the workspace path so the caller can read produced artifacts.
    """
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
    """Return the last `tail` log lines for a cascade task, chronological order.

    Useful for inspecting what happened during a long async run started with
    sync=False, or for diagnosing why a task failed / was cancelled.
    """
    s = settings()
    store = await Store.open(s.cascade_db_path)
    try:
        entries = await store.tail_logs(task_id, n=tail)
        return [f"[{e.level}] {e.message}" for e in entries]
    finally:
        await store.close()


@mcp.tool()
async def cascade_cancel(task_id: str) -> dict:
    """Cancel a running cascade task by setting its cancel-event.

    Only effective for tasks started in this MCP-server process via
    run_cascade_tool(sync=False). Tasks from the Telegram-bot or CLI run
    in a different process and won't be reachable here.
    """
    if task_id not in _RUNNING:
        return {"task_id": task_id, "cancelled": False, "reason": "not running in this process"}
    _, ev = _RUNNING[task_id]
    ev.set()
    return {"task_id": task_id, "cancelled": True}


@mcp.tool()
async def cascade_history(limit: int = 10) -> list[dict]:
    """Return the last `limit` cascade tasks across all interfaces, newest first.

    Each entry has task_id, status, the truncated task text, iteration
    count, and created_at (unix epoch). Use cascade_status(task_id) for a
    full view of a specific task.
    """
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


@mcp.tool()
async def cascade_progress(
    task_id: str,
    after_ts: float = 0.0,
    lang: str = "de",
    n: int = 200,
) -> dict:
    """Render the latest progress events for a task as ready-to-display
    milestone lines. Same formatter the Telegram bot uses, so the
    `/cascade` slash-command shows IDENTICAL output.

    Args:
      task_id: the run to inspect.
      after_ts: only return milestones logged after this unix-ts —
        used as a cursor by the polling loop (pass `last_ts` from
        the previous call). 0.0 = from the start.
      lang: `"de"` or `"en"`. Defaults to German.
      n: how many recent log entries to scan (max 500).

    Returns:
      {
        "status": "<task status>",
        "iteration": <int>,
        "lines": [<rendered milestone line>, ...],
        "last_ts": <highest ts in this batch>,
        "task_id": <id>,
      }
    """
    n = max(1, min(int(n), 500))
    s = settings()
    store = await Store.open(s.cascade_db_path)
    try:
        t = await store.get_task(task_id)
        if t is None:
            return {"error": "not found", "task_id": task_id}
        entries = await store.tail_logs(task_id, n=n)
    finally:
        await store.close()

    from cascade.progress_format import format_milestone, parse_log_message
    out_lines: list[str] = []
    last_ts = float(after_ts)
    for e in entries:
        if e.ts <= after_ts:
            continue
        if e.ts > last_ts:
            last_ts = e.ts
        parsed = parse_log_message(e.message)
        if not parsed:
            continue
        event, payload = parsed
        for line in format_milestone(event, payload, lang=lang):
            if line:
                out_lines.append(line)
    return {
        "task_id": task_id,
        "status": t.status,
        "iteration": t.iteration,
        "lines": out_lines,
        "last_ts": last_ts,
    }


@mcp.tool()
async def cascade_summary(task_id: str, include_diff: bool = False) -> dict:
    """One-shot status + plan + changed-files + reviewer feedback + diff
    excerpt for a cascade task. Used by the `/cascade` slash-command so
    Claude Code only has to make a single MCP call after a run instead
    of stitching cascade_status + cascade_logs + filesystem reads.

    Args:
      task_id: the run to inspect.
      include_diff: when True, also include the FULL git diff (capped
        at ~50 KB). Off by default — call cascade_logs(task_id) or read
        the workspace files directly when you need the full thing.

    Returns a dict with these keys (some may be missing on failure):
      task_id, status, iteration, summary, workspace, created_at,
      completed_at, plan {summary, steps, acceptance_criteria,
      quality_checks, subtasks}, changed_files, recent_reviews
      (list of {iteration, passed, feedback, severity}),
      diff_excerpt (always — first ~6 KB of the cumulative diff),
      diff (only when include_diff=True).
    """
    import json as _json
    s = settings()
    store = await Store.open(s.cascade_db_path)
    try:
        t = await store.get_task(task_id)
        if t is None:
            return {"error": "not found", "task_id": task_id}
        out: dict = {
            "task_id": t.id,
            "status": t.status,
            "iteration": t.iteration,
            "summary": t.result_summary,
            "workspace": t.workspace_path,
            "created_at": t.created_at,
            "completed_at": t.completed_at,
        }
        # Plan from iteration 0
        try:
            iters = await store.list_iterations(task_id)
        except Exception:
            iters = []
        if iters and iters[0].n == 0 and iters[0].implementer_output:
            try:
                plan_data = _json.loads(iters[0].implementer_output)
                out["plan"] = {
                    "summary": plan_data.get("summary"),
                    "steps": plan_data.get("steps", [])[:10],
                    "acceptance_criteria": plan_data.get("acceptance_criteria", [])[:10],
                    "quality_checks": [
                        {"name": c.get("name"), "command": c.get("command")}
                        for c in (plan_data.get("quality_checks") or [])[:8]
                    ],
                    "subtasks": [
                        {"name": st.get("name"), "summary": (st.get("summary") or "")[:200]}
                        for st in (plan_data.get("subtasks") or [])
                    ],
                }
            except Exception:
                pass
        # Last 5 reviewer feedbacks (most-recent last)
        reviews = []
        for it in iters[-6:]:  # skip iter 0 (the plan) when present
            if it.n == 0 or it.reviewer_pass is None:
                continue
            reviews.append({
                "iteration": it.n,
                "passed": bool(it.reviewer_pass),
                "feedback": (it.reviewer_feedback or "")[:600],
            })
        if reviews:
            out["recent_reviews"] = reviews
        # Diff: cap aggressively so the MCP response stays tractable.
        # Read the workspace's cumulative diff if it still exists on disk.
        diff_text = ""
        if t.workspace_path:
            try:
                from pathlib import Path as _Path
                from cascade.workspace import Workspace
                ws = Workspace.attach(_Path(t.workspace_path))
                diff_text = ws.diff_cumulative(max_bytes=50_000)
                out["changed_files"] = ws.changed_paths()
            except Exception:
                pass
        if diff_text:
            out["diff_excerpt"] = diff_text[:6000]
            if include_diff:
                out["diff"] = diff_text
        return out
    finally:
        await store.close()


def _result_dict(r) -> dict:
    return {
        "task_id": r.task_id,
        "status": r.status,
        "iterations": r.iterations,
        "summary": r.summary,
        "workspace": str(r.workspace_path),
        "changed_files": r.changed_files,
        "diff_chars": len(r.diff),
        "error": r.error,
    }


@mcp.tool()
async def cascade_resume(task_id: str, sync: bool = True, timeout_s: int = 600) -> dict:
    """Resume an interrupted cascade task.

    Re-uses the original task_text and continues the iteration loop. Useful
    after a bot/server crash where mark_running_as_interrupted swept the
    task to status='interrupted'. sync=True blocks until done or timeout.
    """
    s = settings()
    store = await Store.open(s.cascade_db_path)
    try:
        t = await store.get_task(task_id)
    finally:
        await store.close()
    if t is None:
        return {"error": "not found", "task_id": task_id}

    cancel = asyncio.Event()
    coro = run_cascade(
        task=t.task_text,
        source="mcp",
        repo=Path(t.repo_path) if t.repo_path else None,
        s=s,
        cancel_event=cancel,
        resume_task_id=task_id,
    )
    if sync:
        try:
            result = await asyncio.wait_for(coro, timeout=timeout_s)
        except asyncio.TimeoutError:
            cancel.set()
            return {"status": "timeout", "task_id": task_id}
        return _result_dict(result)
    task_obj = asyncio.create_task(coro)
    _RUNNING[task_id] = (task_obj, cancel)
    task_obj.add_done_callback(lambda _t, k=task_id: _RUNNING.pop(k, None))
    return {"task_id": task_id, "status": "resuming", "sync": False}


@mcp.tool()
async def cascade_dryrun(task: str) -> dict:
    """Plan-only: invoke the planner without launching the implementer/reviewer.

    Returns the structured plan (summary, steps, files_to_touch,
    acceptance_criteria, repo decision, quality_checks) so a caller can
    preview what cascade *would* do before paying for the full loop.
    """
    s = settings()
    from cascade.agents.planner import call_planner
    from cascade.repo_resolver import discover_local_repos, repos_for_planner_prompt

    repos = await asyncio.to_thread(discover_local_repos)
    block = repos_for_planner_prompt(repos, task)
    try:
        plan = await call_planner(task, repo_candidates_block=block, s=s)
    except Exception as e:
        return {"error": str(e)}
    return plan.model_dump()


@mcp.tool()
async def cascade_skills_list() -> list[dict]:
    """List all skills the user has accepted from auto-suggestions.

    Skills are reusable parametrised task templates. Run one with
    cascade_skill_run(name=..., args={...}).
    """
    s = settings()
    store = await Store.open(s.cascade_db_path)
    try:
        skills = await store.list_skills()
        return [
            {
                "name": sk["name"],
                "description": sk.get("description"),
                "task_template": sk["task_template"],
                "usage_count": sk.get("usage_count", 0),
                "created_at": sk.get("created_at"),
            }
            for sk in skills
        ]
    finally:
        await store.close()


@mcp.tool()
async def cascade_skill_run(
    name: str,
    args: dict | None = None,
    sync: bool = True,
    timeout_s: int = 600,
    repo: str | None = None,
) -> dict:
    """Run a saved skill, filling its task_template placeholders with `args`.

    `args` is a {placeholder: value} dict (e.g. {"file": "foo.py",
    "aspect": "edge cases"}). Falls back to free-form append if formatting
    fails. Increments the skill's usage_count.
    """
    s = settings()
    store = await Store.open(s.cascade_db_path)
    try:
        sk = await store.get_skill_by_name(name)
        if not sk:
            return {"error": "skill not found", "name": name}
        await store.increment_skill_usage(name)
    finally:
        await store.close()

    args = args or {}
    template = sk["task_template"]
    try:
        task_text = template.format(**args)
    except (KeyError, IndexError):
        task_text = template + ("\n\n" + " ".join(f"{k}={v}" for k, v in args.items()) if args else "")

    return await run_cascade_tool(
        task=task_text, repo=repo, sync=sync, timeout_s=timeout_s
    )


def main() -> None:
    from cascade.logging_config import setup_logging
    setup_logging()
    mcp.run()


if __name__ == "__main__":
    main()
