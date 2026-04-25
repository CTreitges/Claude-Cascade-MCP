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
from .repo_resolver import discover_local_repos, repos_for_planner_prompt, resolve_repo
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
    changed_files: list[str] = field(default_factory=list)
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
    planner_model: str | None = None,
    reviewer_model: str | None = None,
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
    if planner_model or reviewer_model:
        # Apply per-call overrides without mutating the shared singleton:
        # build a derived Settings that the agents will receive.
        s = s.model_copy(update={
            **({"cascade_planner_model": planner_model} if planner_model else {}),
            **({"cascade_reviewer_model": reviewer_model} if reviewer_model else {}),
        })
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

        # Recall context (best-effort)
        recall = await recall_context(task)
        if recall:
            await _log(store, task_id, "info", f"recall: {recall[:200]}…")

        # Discover local repos so the planner can refer to them by absolute path.
        # Only relevant when the caller didn't pin a repo themselves.
        repo_candidates_block: str | None = None
        if repo is None:
            try:
                local_repos = await asyncio.to_thread(discover_local_repos)
                if local_repos:
                    repo_candidates_block = repos_for_planner_prompt(local_repos, task)
                    await _log(
                        store, task_id, "info",
                        f"discovered {len(local_repos)} local repos for planner",
                    )
            except Exception as e:
                await _log(store, task_id, "warn", f"repo discovery failed: {e}")

        await _check_cancel(cancel_event, store, task_id)

        # Plan
        await _emit(progress, store, task_id, "planning", {})
        try:
            # Workspace doesn't exist yet at planning time — create a placeholder
            # path only for error-reporting purposes if the planner itself fails.
            placeholder_root = s.workspaces_dir / task_id
            plan = await call_planner(
                task,
                attachments=attachments,
                recall_context=recall,
                repo_candidates_block=repo_candidates_block,
                s=s,
            )
        except Exception as e:
            # Need a Workspace for the failure path's CascadeResult — make a tmp one.
            placeholder_root.mkdir(parents=True, exist_ok=True)
            ws_err = Workspace(placeholder_root)
            return await _fail(store, task_id, ws_err, "planner failed", e, progress)

        # Resolve workspace based on caller's --repo (highest priority) or planner's hint.
        if repo:
            ws = Workspace.attach(Path(repo))
            await _log(store, task_id, "warn", f"using caller-pinned repo: {repo}")
        else:
            resolved = await resolve_repo(
                plan.repo, workspaces_root=s.workspaces_dir, task_id=task_id
            )
            await _log(
                store, task_id, "info",
                f"repo resolution: source={resolved.source} path={resolved.path} note={resolved.note}",
            )
            if resolved.path is not None:
                ws = Workspace.attach(resolved.path)
            else:
                ws = Workspace.create(s.workspaces_dir, task_id=task_id)
        await store.update_task(task_id, workspace_path=str(ws.root), status="running")

        await store.record_iteration(task_id, 0, implementer_output=plan.model_dump_json())
        await _emit(
            progress,
            store,
            task_id,
            "planned",
            {"summary": plan.summary, "steps": plan.steps, "repo": plan.repo.model_dump()},
        )

        # Loop
        last_review: ReviewResult | None = None
        last_check_results: list = []
        feedback: str | None = None
        consecutive_failures = 0
        replans_done = 0
        iter_n = start_iter
        for iter_n in range(start_iter, s.cascade_max_iterations + 1):
            await _check_cancel(cancel_event, store, task_id)
            await store.update_task(task_id, iteration=iter_n)

            # Implementer
            await _emit(progress, store, task_id, "implementing", {"iteration": iter_n})
            ws_files = ws.list_files()
            # Auto-load file contents for read-only context. Without this the
            # implementer is blind to existing code and can only do greenfield
            # writes — useless for "analyse this repo" or "fix bug in foo.py".
            ctx_files = ws.candidate_context_files(plan.files_to_touch, limit=12)
            file_contents = ws.read_files(ctx_files) if ctx_files else {}
            try:
                impl: ImplementerOutput = await call_implementer(
                    plan,
                    workspace_files=ws_files,
                    feedback=feedback,
                    iteration=iter_n,
                    model=implementer_model,
                    provider=implementer_provider,
                    s=s,
                    file_contents=file_contents,
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

            # Quality checks — objective, scriptable verifications declared
            # by the planner. They run in the workspace and their results
            # feed into the reviewer prompt + override pass=true if any failed.
            check_results = []
            for chk in plan.quality_checks:
                try:
                    res = await ws.run_check(chk)
                except Exception as e:
                    from .workspace import CheckResult as _CR
                    res = _CR(chk.name, False, -3, f"runner-error: {e}", 0.0)
                check_results.append(res)
                await _log(
                    store, task_id, "info" if res.ok else "warn",
                    f"check[{iter_n}/{chk.name}] ok={res.ok} exit={res.exit_code} "
                    f"out={(res.output or '').strip()[:200]!r}",
                )
            await _emit(
                progress, store, task_id, "checks_run",
                {"iteration": iter_n, "checks": [{"name": r.name, "ok": r.ok} for r in check_results]},
            )

            # Review
            diff = ws.diff()
            await _emit(progress, store, task_id, "reviewing", {"iteration": iter_n})
            try:
                review: ReviewResult = await call_reviewer(plan, diff, check_results=check_results, s=s)
            except Exception as e:
                return await _fail(store, task_id, ws, f"reviewer iter {iter_n}", e, progress)

            # Hard gate: any failed check overrides a `pass=true` from the reviewer.
            failing_checks = [r.name for r in check_results if not r.ok]
            if failing_checks and review.passed:
                review = review.model_copy(update={
                    "passed": False,
                    "feedback": (
                        f"Quality checks failed: {', '.join(failing_checks)}. "
                        f"{review.feedback or ''}"
                    ).strip(),
                })

            last_check_results = check_results

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
                    changed_files=ws.list_files(),
                )

            # not passed → next iteration
            feedback = review.feedback
            consecutive_failures += 1
            await _emit(
                progress,
                store,
                task_id,
                "iteration_failed",
                {"iteration": iter_n, "feedback": feedback},
            )

            # Re-plan trigger: if we've been stuck for N consecutive iterations
            # AND we still have replan budget AND there are more iterations left
            # to actually use the new plan, ask the planner to rewrite the plan
            # (especially the quality_checks). Solves the loop deadlock when
            # the plan itself is wrong (e.g. python vs python3).
            iters_left = s.cascade_max_iterations - iter_n
            if (
                consecutive_failures >= s.cascade_replan_after_failures
                and replans_done < s.cascade_replan_max
                and iters_left >= 1
            ):
                replan_block = _build_replan_feedback(plan, await store.list_iterations(task_id))
                await _emit(progress, store, task_id, "replanning",
                            {"after_iteration": iter_n, "replans_done": replans_done})
                try:
                    new_plan = await call_planner(
                        task,
                        attachments=attachments,
                        recall_context=recall,
                        repo_candidates_block=repo_candidates_block,
                        replan_feedback=replan_block,
                        s=s,
                    )
                    plan = new_plan
                    feedback = None  # fresh slate; new plan replaces old feedback
                    consecutive_failures = 0
                    replans_done += 1
                    await store.record_iteration(
                        task_id, 0, implementer_output=plan.model_dump_json()
                    )
                    await _emit(
                        progress, store, task_id, "replanned",
                        {"summary": plan.summary, "checks": [c.name for c in plan.quality_checks]},
                    )
                    await _log(
                        store, task_id, "info",
                        f"replanned after iter {iter_n} (replans_done={replans_done})",
                    )
                except Exception as e:
                    await _log(store, task_id, "warn", f"replan failed, continuing with old plan: {e}")

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
            changed_files=ws.list_files(),
        )

    finally:
        if own_store:
            await store.close()


# ---------- helpers ----------


def _build_replan_feedback(prev_plan: Plan, iter_history: list) -> str:
    """Render a compact summary of the previous plan + iteration failures
    so the planner can produce a corrected plan."""
    lines = [
        "PREVIOUS PLAN (now superseded):",
        f"  summary: {prev_plan.summary}",
        f"  steps: {prev_plan.steps}",
        f"  files_to_touch: {prev_plan.files_to_touch}",
        f"  acceptance_criteria: {prev_plan.acceptance_criteria}",
        f"  quality_checks:",
    ]
    for qc in prev_plan.quality_checks:
        lines.append(f"    - name={qc.name!r} command={qc.command!r}")
    lines.append("\nITERATION HISTORY:")
    runtime_iters = [i for i in iter_history if i.n > 0]
    for it in runtime_iters[-4:]:  # last few iterations only
        lines.append(
            f"  iter {it.n}: pass={it.reviewer_pass} "
            f"feedback={(it.reviewer_feedback or '').strip()[:300]!r}"
        )
    lines.append(
        "\nLikely root cause to consider: were the quality_checks commands "
        "correct for this Linux runner? (Use python3, not python; absolute "
        "paths; cwd is the repo root.) Adjust the plan accordingly."
    )
    return "\n".join(lines)


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
