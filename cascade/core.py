"""Cascade orchestrator: Plan → (Implement → Review)×N → Done."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Literal

from .agents.implementer import ImplementerOutput, call_implementer
from .agents.planner import Plan, call_planner
from .agents.reviewer import ReviewResult, call_reviewer
from .config import Settings, settings
from .memory import recall_context, remember_finding
from .store import Store, Task
from .workspace import Workspace, cleanup_old_workspaces

log = logging.getLogger("cascade")


ProgressEvent = Literal[
    "started",
    "planning",
    "planned",
    "implementing",
    "implemented",
    "reviewing",
    "reviewed",
    "iteration_failed",
    "done",
    "failed",
    "cancelled",
    "log",
]

ProgressCallback = Callable[[str, ProgressEvent, dict], Awaitable[None]]


async def _noop(_task_id: str, _event: ProgressEvent, _payload: dict) -> None:
    return


@dataclass
class CascadeResult:
    task_id: str
    status: str
    iterations: int
    plan: Plan | None
    final_review: ReviewResult | None
    workspace_path: Path
    summary: str
    diff: str = ""
    cancelled: bool = False
    error: str | None = None
    metadata: dict = field(default_factory=dict)


# ---------- the loop ----------


async def run_cascade(
    *,
    task: str,
    source: Literal["mcp", "telegram", "cli"] = "cli",
    repo: Path | None = None,
    attachments: list[Path] | None = None,
    implementer_model: str | None = None,
    implementer_provider: str | None = None,
    implementer_tools: Literal["fileops", "mcp"] | None = None,
    progress: ProgressCallback = _noop,
    s: Settings | None = None,
    store: Store | None = None,
    cancel_event: asyncio.Event | None = None,
    resume_task_id: str | None = None,
) -> CascadeResult:
    """Single end-to-end cascade run.

    If `store` is provided, the run is persisted (tasks/iterations/logs) and
    `progress` callbacks are emitted alongside DB writes.
    `cancel_event.set()` mid-run aborts cleanly between agent calls.
    """
    s = s or settings()
    cancel_event = cancel_event or asyncio.Event()
    own_store = store is None
    if store is None:
        store = await Store.open(s.cascade_db_path)

    try:
        if resume_task_id:
            task_id = resume_task_id
            existing = await store.get_task(task_id)
            if existing is None:
                raise ValueError(f"Cannot resume — task {task_id!r} not found")
            start_iter = max(existing.iteration, 1)
            await store.update_task(task_id, status="running")
        else:
            task_id = await store.create_task(
                source=source,
                task_text=task,
                repo_path=str(repo) if repo else None,
                implementer_model=implementer_model or s.cascade_implementer_model,
                implementer_tools=implementer_tools or s.cascade_implementer_tools,
            )
            start_iter = 1

        await _emit(progress, store, task_id, "started", {"task": task})

        # Workspace
        if repo:
            ws = Workspace.attach(Path(repo))
            await _log(store, task_id, "warn", f"running against existing repo: {repo}")
        else:
            ws = Workspace.create(s.workspaces_dir, task_id=task_id)
        await store.update_task(task_id, workspace_path=str(ws.root), status="running")

        # Recall context (best-effort)
        recall = await recall_context(task)
        if recall:
            await _log(store, task_id, "info", f"recall: {recall[:200]}…")

        await _check_cancel(cancel_event, store, task_id)

        # Plan
        await _emit(progress, store, task_id, "planning", {})
        try:
            plan = await call_planner(
                task, attachments=attachments, recall_context=recall, s=s
            )
        except Exception as e:
            return await _fail(store, task_id, ws, "planner failed", e, progress)
        await store.record_iteration(task_id, 0, implementer_output=plan.model_dump_json())
        await _emit(
            progress,
            store,
            task_id,
            "planned",
            {"summary": plan.summary, "steps": plan.steps},
        )

        # Loop
        last_review: ReviewResult | None = None
        feedback: str | None = None
        iter_n = start_iter
        for iter_n in range(start_iter, s.cascade_max_iterations + 1):
            await _check_cancel(cancel_event, store, task_id)
            await store.update_task(task_id, iteration=iter_n)

            # Implementer
            await _emit(progress, store, task_id, "implementing", {"iteration": iter_n})
            try:
                impl: ImplementerOutput = await call_implementer(
                    plan,
                    workspace_files=ws.list_files(),
                    feedback=feedback,
                    iteration=iter_n,
                    model=implementer_model,
                    provider=implementer_provider,
                    s=s,
                )
            except Exception as e:
                return await _fail(store, task_id, ws, f"implementer iter {iter_n}", e, progress)

            op_results = ws.apply_ops(impl.ops)
            failed_ops = [r for r in op_results if not r.ok]
            if failed_ops:
                await _log(
                    store,
                    task_id,
                    "warn",
                    f"iter {iter_n}: {len(failed_ops)} ops failed: "
                    + "; ".join(f"{r.op} {r.path}: {r.detail}" for r in failed_ops),
                )

            await _emit(
                progress,
                store,
                task_id,
                "implemented",
                {"iteration": iter_n, "ops": len(impl.ops), "failed": len(failed_ops)},
            )

            await _check_cancel(cancel_event, store, task_id)

            # Review
            diff = ws.diff()
            await _emit(progress, store, task_id, "reviewing", {"iteration": iter_n})
            try:
                review: ReviewResult = await call_reviewer(plan, diff, s=s)
            except Exception as e:
                return await _fail(store, task_id, ws, f"reviewer iter {iter_n}", e, progress)

            await store.record_iteration(
                task_id,
                iter_n,
                implementer_output=json.dumps(
                    {
                        "ops": [op.model_dump() for op in impl.ops],
                        "rationale": impl.rationale,
                        "applied": [r.__dict__ for r in op_results],
                    }
                ),
                reviewer_pass=review.passed,
                reviewer_feedback=review.feedback,
                diff_excerpt=diff[:8000],
            )
            await _emit(
                progress,
                store,
                task_id,
                "reviewed",
                {
                    "iteration": iter_n,
                    "pass": review.passed,
                    "feedback": review.feedback,
                },
            )

            last_review = review
            ws.commit_iteration(iter_n)

            if review.passed:
                summary = f"done after {iter_n} iteration(s)"
                await store.update_task(
                    task_id,
                    status="done",
                    result_summary=summary,
                    completed=True,
                )
                await _emit(progress, store, task_id, "done", {"summary": summary})

                # RLM remember if reviewer flagged something noteworthy
                if review.severity in ("medium", "high"):
                    await remember_finding(
                        f"Cascade task '{task[:80]}': {review.feedback or 'completed cleanly'}"
                    )

                return CascadeResult(
                    task_id=task_id,
                    status="done",
                    iterations=iter_n,
                    plan=plan,
                    final_review=review,
                    workspace_path=ws.root,
                    summary=summary,
                    diff=diff,
                )

            # not passed → next iteration
            feedback = review.feedback
            await _emit(
                progress,
                store,
                task_id,
                "iteration_failed",
                {"iteration": iter_n, "feedback": feedback},
            )

        # Max iterations exhausted
        summary = f"failed after {s.cascade_max_iterations} iterations"
        await store.update_task(
            task_id, status="failed", result_summary=summary, completed=True
        )
        await _emit(progress, store, task_id, "failed", {"summary": summary})
        return CascadeResult(
            task_id=task_id,
            status="failed",
            iterations=iter_n,
            plan=plan,
            final_review=last_review,
            workspace_path=ws.root,
            summary=summary,
            diff=ws.diff(),
        )

    finally:
        if own_store:
            await store.close()


# ---------- helpers ----------


async def _emit(
    cb: ProgressCallback,
    store: Store,
    task_id: str,
    event: ProgressEvent,
    payload: dict,
) -> None:
    try:
        await cb(task_id, event, payload)
    except Exception as e:  # callbacks must never crash the run
        log.warning("progress callback failed: %s", e)
    await store.log(task_id, "info", f"{event}: {json.dumps(payload, default=str)[:300]}")


async def _log(store: Store, task_id: str, level, message: str) -> None:
    await store.log(task_id, level, message)


async def _check_cancel(ev: asyncio.Event, store: Store, task_id: str) -> None:
    if ev.is_set():
        await store.update_task(task_id, status="cancelled", completed=True)
        await store.log(task_id, "warn", "cancelled by request")
        raise asyncio.CancelledError()


async def _fail(
    store: Store,
    task_id: str,
    ws: Workspace,
    where: str,
    err: Exception,
    progress: ProgressCallback,
) -> CascadeResult:
    msg = f"{where}: {err}"
    await store.update_task(task_id, status="failed", result_summary=msg, completed=True)
    await store.log(task_id, "error", msg)
    await _emit(progress, store, task_id, "failed", {"summary": msg})
    return CascadeResult(
        task_id=task_id,
        status="failed",
        iterations=0,
        plan=None,
        final_review=None,
        workspace_path=ws.root,
        summary=msg,
        error=str(err),
    )


@asynccontextmanager
async def maintenance(s: Settings | None = None) -> AsyncIterator[None]:
    """Run periodic workspace cleanup in the background."""
    s = s or settings()

    async def _loop() -> None:
        while True:
            try:
                await cleanup_old_workspaces(s.workspaces_dir, s.cascade_workspace_retention_days)
            except Exception as e:
                log.warning("workspace cleanup failed: %s", e)
            await asyncio.sleep(6 * 3600)

    t = asyncio.create_task(_loop())
    try:
        yield
    finally:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass


# ---------- CLI ----------


async def _cli_run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    async def cli_progress(task_id: str, event: str, payload: dict) -> None:
        print(f"[{task_id}] {event}: {json.dumps(payload, default=str)}")

    result = await run_cascade(
        task=args.task,
        source="cli",
        repo=Path(args.repo).resolve() if args.repo else None,
        implementer_model=args.model,
        implementer_provider=args.provider,
        implementer_tools=args.tools,
        progress=cli_progress,
    )
    print("\n=== RESULT ===")
    print(f"id:         {result.task_id}")
    print(f"status:     {result.status}")
    print(f"iterations: {result.iterations}")
    print(f"workspace:  {result.workspace_path}")
    print(f"summary:    {result.summary}")
    return 0 if result.status == "done" else 1


def cli_main() -> None:
    p = argparse.ArgumentParser(prog="cascade")
    p.add_argument("task", help="Task description in natural language")
    p.add_argument("--repo", help="Existing repo to operate inside (default: tmp workspace)")
    p.add_argument("--model", help="Override implementer model")
    p.add_argument("--provider", choices=["ollama", "openai_compatible"])
    p.add_argument("--tools", choices=["fileops", "mcp"])
    args = p.parse_args()
    sys.exit(asyncio.run(_cli_run(args)))


if __name__ == "__main__":
    cli_main()
