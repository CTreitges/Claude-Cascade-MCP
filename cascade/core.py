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
from .healing import HealingMonitor, HealingState
from .memory import remember_decision, remember_finding
from .repo_resolver import resolve_repo
from .skill_suggester import maybe_suggest_skill
from .store import Store, Task
from .workspace import Workspace, cleanup_old_workspaces

log = logging.getLogger("cascade")


ProgressEvent = Literal[
    "started",
    "planning",
    "planned",
    "implementing",
    "implemented",
    "checks_run",
    "reviewing",
    "reviewed",
    "replanning",
    "replanned",
    "iteration_failed",
    "ask_user",
    "skill_suggested",
    "done",
    "failed",
    "cancelled",
    "log",
    "waiting_for_session",
    "hard_stuck",
]

ProgressCallback = Callable[[str, ProgressEvent, dict], Awaitable[None]]


async def _noop(_task_id: str, _event: ProgressEvent, _payload: dict) -> None:
    return


# Subset of Settings that materially affect a run's behavior. Snapshotted into
# tasks.metadata at create-time so /resume can detect when user changed any
# of them in the meantime and switch to a fresh-restart-with-context flow.
_SNAPSHOT_KEYS = (
    "cascade_replan_max",
    "cascade_max_iterations",
    "cascade_replan_after_failures",
    "cascade_planner_model",
    "cascade_implementer_model",
    "cascade_reviewer_model",
    "cascade_planner_effort",
    "cascade_reviewer_effort",
    "cascade_implementer_effort",
    "cascade_triage_enabled",
    "cascade_auto_skill_suggest",
    "cascade_context7_enabled",
    "cascade_websearch_enabled",
)


def _snapshot_settings(s: Settings) -> dict:
    """Capture the run-affecting settings so we can detect drift on /resume."""
    return {k: getattr(s, k, None) for k in _SNAPSHOT_KEYS}


def settings_snapshot_differs(snapshot: dict | None, current: Settings) -> list[str]:
    """Return the keys whose value drifted vs. the original snapshot.
    Empty list when nothing changed (or when the task pre-dates snapshotting)."""
    if not snapshot:
        return []
    drift = []
    for k in _SNAPSHOT_KEYS:
        if snapshot.get(k) != getattr(current, k, None):
            drift.append(k)
    return drift


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
    planner_effort: str | None = None,
    reviewer_effort: str | None = None,
    triage_effort: str | None = None,  # picked up by Settings; bot.on_text uses it for its own triage call
    implementer_effort: str | None = None,
    planner_temperature: float | None = None,
    implementer_temperature: float | None = None,
    reviewer_temperature: float | None = None,
    replan_max: int | None = None,
    max_iterations: int | None = None,
    lang: str = "en",
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
    overrides: dict = {}
    if planner_model:
        overrides["cascade_planner_model"] = planner_model
    if reviewer_model:
        overrides["cascade_reviewer_model"] = reviewer_model
    if planner_effort:
        overrides["cascade_planner_effort"] = planner_effort
    if reviewer_effort:
        overrides["cascade_reviewer_effort"] = reviewer_effort
    if triage_effort:
        overrides["cascade_triage_effort"] = triage_effort
    if implementer_effort:
        overrides["cascade_implementer_effort"] = implementer_effort
    if replan_max is not None:
        overrides["cascade_replan_max"] = replan_max
    if max_iterations is not None:
        overrides["cascade_max_iterations"] = max_iterations
    if overrides:
        s = s.model_copy(update=overrides)
    cancel_event = cancel_event or asyncio.Event()
    own_store = store is None
    if store is None:
        store = await Store.open(s.cascade_db_path)

    # Carry resume state out of the conditional so later code can check
    # whether to restore a saved plan / pin a workspace / skip done sub-tasks.
    resumed_plan: Plan | None = None
    resumed_completed_subtasks: set[str] = set()
    resumed_workspace_path: Path | None = None
    existing: Task | None = None
    try:
        if resume_task_id:
            task_id = resume_task_id
            existing = await store.get_task(task_id)
            if existing is None:
                raise ValueError(f"Cannot resume — task {task_id!r} not found")
            start_iter = max(existing.iteration, 1)
            await store.update_task(task_id, status="running")

            # Reload the saved plan from iteration 0 — saves a wasted Planner
            # call that would just produce a near-identical plan and reset
            # the run's snapshotted settings.
            #
            # Hardening: if iteration 0's implementer_output is missing or
            # corrupt (older DB rows, manual edits, partial write before a
            # SIGKILL), we DON'T crash — we just leave `resumed_plan=None`
            # so the loop falls through to a fresh planner call. The user
            # gets a one-line note in the task log explaining the fallback.
            try:
                iters = await store.list_iterations(task_id)
                if iters and iters[0].n == 0 and iters[0].implementer_output:
                    try:
                        resumed_plan = Plan.model_validate_json(iters[0].implementer_output)
                    except Exception as plan_err:
                        log.warning(
                            "resume: iteration 0 plan corrupt (%s) — "
                            "will re-plan from scratch", plan_err,
                        )
                        await store.log(
                            task_id, "warn",
                            f"resume: iteration 0 plan was corrupt — "
                            f"falling back to fresh planning ({plan_err})",
                        )
                # Mark every subtask whose latest iteration was reviewer-pass
                # as already-done so the supervisor skips it on resume.
                last_per_sub: dict[str, bool] = {}
                for it in iters:
                    if it.n == 0:
                        continue
                    try:
                        data = json.loads(it.implementer_output or "{}")
                        sub = data.get("subtask")
                        if sub:
                            last_per_sub[sub] = bool(it.reviewer_pass)
                    except Exception:
                        continue
                resumed_completed_subtasks = {
                    sub for sub, ok in last_per_sub.items() if ok
                }
            except Exception as e:
                log.warning("resume: could not restore plan/subtask state: %s", e)
            if existing.workspace_path:
                resumed_workspace_path = Path(existing.workspace_path)
        else:
            task_id = await store.create_task(
                source=source,
                task_text=task,
                repo_path=str(repo) if repo else None,
                implementer_model=implementer_model or s.cascade_implementer_model,
                implementer_tools=implementer_tools or s.cascade_implementer_tools,
                metadata={"start_settings": _snapshot_settings(s)},
            )
            start_iter = 1

        # Healing-Monitor: observes the run and surfaces stuck/permission
        # diagnostics through the regular progress stream. Shadows the user
        # `progress` callback so every emit also updates HealingState.last_*.
        healing_state = HealingState()
        original_progress = progress

        async def _healing_progress(tid, event, payload):
            try:
                # P3.6 bugfix: don't update last_event_at on events the
                # healing monitor itself emitted. Otherwise hard_stuck
                # never fires — every 180s stuck-alert resets the idle
                # timer and the 300s threshold can't be reached. We
                # *do* still record the log text so the diagnostic scan
                # has the latest events.
                kind = (payload or {}).get("kind") or ""
                is_self_emit = (
                    event == "log"
                    and kind in ("stuck-alert", "implementer-stuck",
                                 "permission-issue", "hard-stuck")
                )
                if not is_self_emit:
                    healing_state.mark_event(event)
                msg_text = payload.get("msg") or payload.get("feedback") or ""
                if msg_text:
                    healing_state.mark_log_text(msg_text)
                if event == "reviewed":
                    healing_state.recent_review_feedback = (
                        payload.get("feedback") or ""
                    )
                # Track implementer outputs by serialised diff/ops shape
                # so we can spot 3-in-a-row identical implementer responses
                # (a complement to reviewer-feedback stagnation).
                if event == "implemented":
                    impl_str = json.dumps(payload, sort_keys=True, default=str)
                    healing_state.mark_implementer_output(impl_str)
            except Exception:
                pass
            await original_progress(tid, event, payload)

        progress = _healing_progress

        # Wait-notifier: when any deep agent_chat call hits a rate-limit
        # / session-cap and `with_retry` sleeps, surface that as a
        # `waiting_for_session` progress event so the bot can edit the
        # status message ("⏳ Warte auf nächste Session — noch 2T 14h").
        async def _wait_notifier(seconds: float, attempt: int, reason: str) -> None:
            try:
                await _emit(
                    progress, store, task_id, "waiting_for_session",
                    {
                        "seconds": int(seconds),
                        "attempt": attempt,
                        "reason": reason[:200],
                        # task_id duplicated in payload so the shared
                        # progress_format.py can render the live-switch tip
                        # (`/stop <id>` → `/models` → `/resume <id>`).
                        "task_id": task_id,
                    },
                )
            except Exception:
                pass

        from .rate_limit import WAIT_NOTIFIER
        _wait_token = WAIT_NOTIFIER.set(_wait_notifier)

        healing_monitor = HealingMonitor(healing_state, progress, task_id)
        await healing_monitor.__aenter__()
        try:
            await _emit(progress, store, task_id, "started", {"task": task})
        except Exception:
            await healing_monitor.__aexit__(None, None, None)
            raise

        # Bind a logging callback once so the setup helpers below don't
        # have to know about Store / task_id internals.
        async def _on_log(level: str, msg: str) -> None:
            await _log(store, task_id, level, msg)

        # Best-effort context-gathering — recall + external research +
        # repo-style probe + local-repo discovery. Helpers in
        # `cascade._core_setup` keep this body readable; behaviour is
        # identical to the inline blocks they replaced.
        from ._core_setup import (
            append_repo_style_hints,
            discover_repo_candidates_for_planner,
            fetch_recall_context,
            gather_external_research,
        )
        recall = await fetch_recall_context(task, on_log=_on_log)
        external_context: str | None = await gather_external_research(
            task, s=s, on_log=_on_log,
        )
        external_context = await append_repo_style_hints(
            external_context, repo=repo, lang=lang, on_log=_on_log,
        )
        repo_candidates_block: str | None = await discover_repo_candidates_for_planner(
            task, repo=repo, on_log=_on_log,
        )

        await _check_cancel(cancel_event, store, task_id)

        # Plan — but skip the planner call entirely on resume if we have
        # the previously-saved plan in iteration 0.
        await _emit(progress, store, task_id, "planning", {})
        placeholder_root = s.workspaces_dir / task_id
        if resumed_plan is not None:
            plan = resumed_plan
            await _log(
                store, task_id, "info",
                "resume: restored saved plan from iteration 0 (no re-planning).",
            )
        else:
            try:
                if getattr(s, "cascade_multiplan_enabled", False):
                    from .multiplan import call_planner_multi
                    plan, pick_reason = await call_planner_multi(
                        task,
                        attachments=attachments,
                        recall_context=recall,
                        repo_candidates_block=repo_candidates_block,
                        external_context=external_context,
                        base_temperature=planner_temperature,
                        lang=lang,
                        s=s,
                    )
                    await _log(
                        store, task_id, "info",
                        f"multiplan picked: {pick_reason}",
                    )
                else:
                    plan = await call_planner(
                        task,
                        attachments=attachments,
                        recall_context=recall,
                        repo_candidates_block=repo_candidates_block,
                        external_context=external_context,
                        temperature=planner_temperature,
                        lang=lang,
                        s=s,
                    )
            except Exception as e:
                placeholder_root.mkdir(parents=True, exist_ok=True)
                ws_err = Workspace(placeholder_root)
                return await _fail(store, task_id, ws_err, "planner failed", e, progress)
            # Auto-augment with py_compile / ruff if the plan touches .py
            # files and the planner forgot — raises the quality bar without
            # nagging the planner about it every single run.
            plan = augment_quality_checks_for_python(plan)

            # P1.1: validate the plan is actually executable before we
            # spin up a workspace and burn implementer-iterations on it.
            # An empty plan (all four lists empty) means the planner
            # returned junk — common on a flaky LLM call. Retry once
            # before failing the task.
            def _plan_is_actionable(p: Plan) -> bool:
                # Truly-empty plans (no signal whatsoever) get rejected.
                # If ANY of these fields has content, the loop has
                # something to chew on — even a bare acceptance criterion
                # gives the implementer + reviewer a contract to verify.
                return bool(
                    p.direct_ops
                    or p.subtasks
                    or p.steps
                    or p.files_to_touch
                    or p.acceptance_criteria
                )

            if not _plan_is_actionable(plan):
                await _log(
                    store, task_id, "warn",
                    "plan validation: empty plan — direct_ops, subtasks, "
                    "steps, files_to_touch all empty. Retrying planner once.",
                )
                try:
                    plan = await call_planner(
                        task,
                        attachments=attachments,
                        recall_context=recall,
                        repo_candidates_block=repo_candidates_block,
                        replan_feedback=(
                            "Your previous plan was empty (no direct_ops, "
                            "subtasks, steps, or files_to_touch). Produce "
                            "an actionable plan with at least the steps "
                            "and files_to_touch fields populated."
                        ),
                        external_context=external_context,
                        temperature=planner_temperature,
                        lang=lang,
                        s=s,
                    )
                    plan = augment_quality_checks_for_python(plan)
                except Exception as e:
                    placeholder_root.mkdir(parents=True, exist_ok=True)
                    ws_err = Workspace(placeholder_root)
                    return await _fail(
                        store, task_id, ws_err,
                        "planner-retry on empty plan", e, progress,
                    )
                if not _plan_is_actionable(plan):
                    placeholder_root.mkdir(parents=True, exist_ok=True)
                    ws_err = Workspace(placeholder_root)
                    return await _fail(
                        store, task_id, ws_err, "plan validation",
                        RuntimeError(
                            "planner returned empty plan twice — refusing "
                            "to start an unrunnable cascade"
                        ),
                        progress,
                    )

        # Resolve workspace. On resume: pin to the existing workspace_path
        # so we don't accidentally jump into a different one and lose state.
        if resumed_workspace_path is not None and resumed_workspace_path.exists():
            ws = Workspace.attach(resumed_workspace_path)
            await _log(
                store, task_id, "info",
                f"resume: re-attached to existing workspace {resumed_workspace_path}",
            )
        elif repo:
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

        # Only re-record iteration 0 on a fresh run — on resume we already
        # have it stored and overwriting would lose timestamp.
        if resumed_plan is None:
            await store.record_iteration(task_id, 0, implementer_output=plan.model_dump_json())
        await _emit(
            progress,
            store,
            task_id,
            "planned",
            {
                "summary": plan.summary,
                "steps": plan.steps,
                "repo": plan.repo.model_dump(),
                "subtasks_count": len(plan.subtasks) if plan.subtasks else 0,
                "subtasks": [st.name for st in (plan.subtasks or [])],
            },
        )

        # ---------- Trivial-Task Shortcut ----------
        # The planner can flag a task as small enough that the full
        # implement-review iter loop is wasteful. We then apply its
        # `direct_ops` once and run a single reviewer pass.
        if plan.direct_ops and not plan.subtasks:
            await _log(
                store, task_id, "info",
                f"trivial-task shortcut: {len(plan.direct_ops)} direct_ops "
                f"({plan.direct_rationale or '—'})",
            )
            await _emit(progress, store, task_id, "log",
                        {"msg": f"trivial shortcut · {len(plan.direct_ops)} ops"})
            try:
                from .workspace import FileOp
                ops = [FileOp.model_validate(o) if not isinstance(o, FileOp) else o
                       for o in plan.direct_ops]
            except Exception as e:
                return await _fail(store, task_id, ws, "direct_ops validation", e, progress)
            op_results = ws.apply_ops(ops)
            await _emit(progress, store, task_id, "implemented",
                        {"iteration": 1, "ops": len(ops),
                         "failed": sum(1 for r in op_results if not r.ok),
                         "subtask": "trivial"})
            ws.commit_iteration(1)
            full_diff = ws.diff_cumulative()
            await _emit(progress, store, task_id, "reviewing",
                        {"iteration": 1, "subtask": "trivial"})
            try:
                review = await call_reviewer(
                    plan, full_diff, check_results=None,
                    external_context=external_context,
                    temperature=reviewer_temperature, lang=lang, task=task, s=s,
                )
            except Exception as e:
                return await _fail(store, task_id, ws, "trivial-shortcut review", e, progress)
            await store.record_iteration(
                task_id, 1,
                implementer_output=json.dumps({
                    "shortcut": True,
                    "ops": [op.model_dump() for op in ops],
                    "rationale": plan.direct_rationale,
                }),
                reviewer_pass=review.passed, reviewer_feedback=review.feedback,
                diff_excerpt=full_diff[:8000],
            )
            await _emit(progress, store, task_id, "reviewed",
                        {"iteration": 1, "pass": review.passed,
                         "feedback": review.feedback, "subtask": "trivial"})
            if review.passed:
                # P1.4: re-run the plan-level quality_checks one last time.
                # Cheap, but catches the rare case where the trivial-shortcut
                # ops broke something the reviewer missed.
                gate_ok, gate_failing = await _final_quality_gate(
                    plan=plan, ws=ws, store=store, task_id=task_id,
                    progress=progress, lang=lang,
                )
                if not gate_ok:
                    summary = (
                        "Final quality gate failed: "
                        + ", ".join(gate_failing)
                    )
                    await store.update_task(
                        task_id, status="failed",
                        result_summary=summary, completed=True,
                    )
                    await _emit(progress, store, task_id, "failed",
                                {"summary": summary})
                    # P3.3: failures are the most instructive runs to
                    # reflect on — capture what went sideways for future
                    # similar tasks.
                    await _try_reflect(
                        task=task, plan=plan, iterations=1,
                        final_diff=full_diff, task_id=task_id,
                        store=store, s=s, lang=lang,
                    )
                    return CascadeResult(
                        task_id=task_id, status="failed", iterations=1,
                        plan=plan, final_review=review, workspace_path=ws.root,
                        summary=summary, diff=full_diff,
                        changed_files=ws.changed_paths(),
                        error="final_quality_gate_failed",
                    )
                summary = f"done via trivial-shortcut ({len(ops)} ops, 1 review)"
                await store.update_task(task_id, status="done",
                                        result_summary=summary, completed=True)
                await _emit(progress, store, task_id, "done", {"summary": summary})
                # P3.3: reflect on trivial-shortcut runs too — they're the
                # cheapest, but lessons about plan-recognition still help
                # future similar tasks.
                await _try_reflect(
                    task=task, plan=plan, iterations=1,
                    final_diff=full_diff, task_id=task_id,
                    store=store, s=s, lang=lang,
                )
                return CascadeResult(
                    task_id=task_id, status="done", iterations=1,
                    plan=plan, final_review=review, workspace_path=ws.root,
                    summary=summary, diff=full_diff, changed_files=ws.changed_paths(),
                )
            # Reviewer rejected → fall through to the regular iter-loop
            # using the rejection feedback as initial input.
            await _log(
                store, task_id, "warn",
                f"trivial-shortcut review rejected: {(review.feedback or '')[:200]} — "
                "falling back to normal iter-loop.",
            )
            # Normalize the plan so the iter-loop has something to chew on.
            plan = plan.model_copy(update={"direct_ops": []})

        # ---------- Auto-decompose supervisor ----------
        # If the planner emitted sub-tasks, run them sequentially on the shared
        # workspace. Each sub-task is its own implement→review mini-loop with a
        # share of the iteration budget. After all sub-tasks pass, a final
        # integration review on the full cumulative diff seals the run.
        if plan.subtasks and s.cascade_auto_decompose:
            from .agents.planner import Plan as _Plan
            max_st = s.cascade_max_subtasks
            subtasks = list(plan.subtasks)[:max_st]
            await _log(
                store, task_id, "info",
                f"supervisor: decomposing into {len(subtasks)} sub-tasks "
                f"(rationale: {plan.decompose_rationale or '—'})",
            )
            sub_iter_budget = max(2, s.cascade_max_iterations // max(1, len(subtasks)))
            # On resume: continue cumulative-iter where we left off so logs
            # and budget tracking stay consistent across restarts.
            cumulative_iter = (existing.iteration if (existing and resume_task_id) else 0)
            all_subs_ok = True
            last_sub_feedback: str | None = None
            if resumed_completed_subtasks:
                await _log(
                    store, task_id, "info",
                    f"resume: skipping already-passed sub-tasks "
                    f"{sorted(resumed_completed_subtasks)}",
                )
            for st_idx, subtask in enumerate(subtasks, start=1):
                if subtask.name in resumed_completed_subtasks:
                    await _emit(progress, store, task_id, "log",
                                {"msg": f"subtask {st_idx}/{len(subtasks)}: "
                                        f"{subtask.name} ✓ already done (resume)"})
                    continue
                await _emit(progress, store, task_id, "log",
                            {"msg": f"subtask {st_idx}/{len(subtasks)}: {subtask.name}"})
                await _log(store, task_id, "info",
                           f"subtask {st_idx}/{len(subtasks)} starting: {subtask.name}")
                sub_plan = _Plan(
                    summary=f"[{st_idx}/{len(subtasks)} · {subtask.name}] {subtask.summary}",
                    steps=subtask.steps,
                    files_to_touch=subtask.files_to_touch,
                    acceptance_criteria=subtask.acceptance_criteria,
                    quality_checks=subtask.quality_checks,
                    repo=plan.repo,
                )
                sub_plan = augment_quality_checks_for_python(sub_plan)
                sub_feedback: str | None = None
                sub_ok = False
                sub_consec_fails = 0
                sub_replans = 0
                # Track per-check-name consecutive failures. Three-stage
                # handling for stuck checks:
                #   - REPAIR_CHECK_THRESHOLD (3 fails): self-heal the check
                #     via a focused planner-LLM call (cascade.check_repair).
                #     Most broken checks (missing --exclude-dir, wrong
                #     scope) get fixed here.
                #   - BROKEN_CHECK_THRESHOLD (4 fails, post-repair):
                #     fall through to a sub-task replan with a hint that
                #     the check is hostile — let the planner consider
                #     dropping or replacing it. Only if the replan-budget
                #     is exhausted do we hard-abort (P1.3 fall-through).
                check_consec_fails: dict[str, int] = {}
                check_repaired_once: set[str] = set()
                check_last_output: dict[str, str] = {}
                REPAIR_CHECK_THRESHOLD = 3
                BROKEN_CHECK_THRESHOLD = 4

                # Empty-Ops loop-breaker (P1.2): if the implementer returns
                # `ops=[]` twice in a row — usually with a rationale like
                # "cannot proceed without read access" — feeding it more
                # iterations only burns tokens. Force a replan instead.
                empty_ops_consec = 0
                for sub_iter in range(1, sub_iter_budget + 1):
                    cumulative_iter += 1
                    await _check_cancel(cancel_event, store, task_id)
                    await store.update_task(task_id, iteration=cumulative_iter)
                    await _emit(progress, store, task_id, "implementing",
                                {"iteration": cumulative_iter, "subtask": subtask.name})
                    ws_files = ws.list_files()
                    ctx_files = ws.candidate_context_files(sub_plan.files_to_touch, limit=12)
                    file_contents = ws.read_files(ctx_files) if ctx_files else {}
                    try:
                        impl = await call_implementer(
                            sub_plan,
                            workspace_files=ws_files,
                            feedback=sub_feedback,
                            iteration=cumulative_iter,
                            model=implementer_model,
                            provider=implementer_provider,
                            effort=s.cascade_implementer_effort or None,
                            temperature=implementer_temperature,
                            external_context=external_context,
                            s=s,
                            file_contents=file_contents,
                        )
                    except Exception as e:
                        return await _fail(store, task_id, ws,
                                           f"implementer subtask={subtask.name} iter={sub_iter}", e, progress)
                    op_results = ws.apply_ops(impl.ops)
                    failed_results = [r for r in op_results if not r.ok]
                    await _emit(progress, store, task_id, "implemented",
                                {"iteration": cumulative_iter, "ops": len(impl.ops),
                                 "failed": len(failed_results),
                                 "subtask": subtask.name})

                    # P2.4: workspace size quota. If the implementer
                    # accidentally writes a 2GB log or recurses into a
                    # generated-file blow-up, abort the whole run before
                    # the disk fills.
                    if s.cascade_workspace_max_bytes > 0:
                        try:
                            ws_size = ws.total_size_bytes()
                        except Exception:
                            ws_size = 0
                        if ws_size > s.cascade_workspace_max_bytes:
                            mb = ws_size / 1_048_576
                            cap_mb = s.cascade_workspace_max_bytes / 1_048_576
                            quota_msg = (
                                f"Workspace-Quota überschritten: {mb:.1f} MB > "
                                f"{cap_mb:.0f} MB Limit (cascade_workspace_max_bytes). "
                                "Wahrscheinlich runaway-Write — Run wird "
                                "abgebrochen damit die Disk nicht volläuft."
                            ) if lang == "de" else (
                                f"Workspace quota exceeded: {mb:.1f} MB > "
                                f"{cap_mb:.0f} MB cap. Likely a runaway write — "
                                "aborting run before the disk fills up."
                            )
                            await _log(store, task_id, "error", quota_msg)
                            await _emit(progress, store, task_id, "log",
                                        {"msg": quota_msg})
                            return await _fail(
                                store, task_id, ws,
                                "workspace quota",
                                RuntimeError(quota_msg),
                                progress,
                            )

                    # P3.6: implementer-stuck → force replan. The healing
                    # monitor flags `implementer_stuck=True` when 3
                    # consecutive impl-outputs have identical hashes
                    # (cascade/healing.py). Reading the flag here lets us
                    # escalate to a planner replan immediately instead of
                    # waiting for the regular consec-fail threshold —
                    # which is moot anyway because identical outputs
                    # produce identical reviewer feedback, locking the
                    # loop.
                    if healing_state.implementer_stuck and sub_replans < s.cascade_replan_max:
                        await _log(
                            store, task_id, "warn",
                            "implementer-stuck flagged by healing — "
                            "forcing sub-task replan",
                        )
                        sub_consec_fails = max(
                            sub_consec_fails,
                            s.cascade_replan_after_failures,
                        )
                        healing_state.implementer_stuck = False  # consumed

                    # P1.2: empty-ops loop-breaker. If the implementer
                    # produces no ops, count consecutive empties and force
                    # a replan after 2 in a row — re-running the same
                    # implementer with the same prompt usually just gets
                    # the same answer. The replan resets sub_consec_fails
                    # so the new plan gets a clean slate.
                    if len(impl.ops) == 0:
                        empty_ops_consec += 1
                        if empty_ops_consec >= 2 and sub_replans < s.cascade_replan_max:
                            await _log(
                                store, task_id, "warn",
                                f"sub-task {subtask.name}: implementer returned "
                                f"0 ops {empty_ops_consec}× in a row "
                                f"(rationale={(impl.rationale or '')[:120]!r}) — "
                                "forcing replan instead of another empty iter",
                            )
                            sub_consec_fails = max(
                                sub_consec_fails,
                                s.cascade_replan_after_failures,
                            )
                            empty_ops_consec = 0
                    else:
                        empty_ops_consec = 0
                    # Make op-failures visible to the NEXT implementer call —
                    # otherwise the implementer only sees the reviewer's
                    # "empty diff" complaint and has no idea WHY its write
                    # was rejected (path-validation, syntax, stub-detection,
                    # find-string-not-unique, …). Without this signal it
                    # often degrades into "no ops" loops.
                    if failed_results:
                        op_failure_block = "OP FAILURES (must address):\n" + "\n".join(
                            f"  - {r.op} {r.path}: {r.detail}"
                            for r in failed_results
                        )
                    else:
                        op_failure_block = None

                    # quality checks (sub-plan's)
                    check_results = []
                    for chk in sub_plan.quality_checks:
                        try:
                            res = await ws.run_check(chk)
                        except Exception as e:
                            from .workspace import CheckResult as _CR
                            res = _CR(chk.name, False, -3, f"runner-error: {e}", 0.0)
                        check_results.append(res)
                    await _emit(progress, store, task_id, "checks_run",
                                {"iteration": cumulative_iter,
                                 "checks": [{"name": r.name, "ok": r.ok} for r in check_results],
                                 "subtask": subtask.name})

                    diff = ws.diff()
                    await _emit(progress, store, task_id, "reviewing",
                                {"iteration": cumulative_iter, "subtask": subtask.name})
                    try:
                        review = await call_reviewer(
                            sub_plan, diff, check_results=check_results,
                            external_context=external_context,
                            temperature=reviewer_temperature, lang=lang, task=task, s=s,
                        )
                    except Exception as e:
                        return await _fail(store, task_id, ws,
                                           f"reviewer subtask={subtask.name} iter={sub_iter}", e, progress)
                    failing = [r.name for r in check_results if not r.ok]
                    if failing and review.passed:
                        review = review.model_copy(update={
                            "passed": False,
                            "feedback": (f"Quality checks failed: {', '.join(failing)}. "
                                         f"{review.feedback or ''}").strip(),
                        })
                    await store.record_iteration(
                        task_id, cumulative_iter,
                        implementer_output=json.dumps({
                            "subtask": subtask.name,
                            "ops": [op.model_dump() for op in impl.ops],
                            "rationale": impl.rationale,
                        }),
                        reviewer_pass=review.passed, reviewer_feedback=review.feedback,
                        diff_excerpt=diff[:8000],
                    )
                    await _emit(progress, store, task_id, "reviewed",
                                {"iteration": cumulative_iter, "pass": review.passed,
                                 "feedback": review.feedback, "subtask": subtask.name})
                    ws.commit_iteration(cumulative_iter)
                    if review.passed:
                        sub_ok = True
                        break
                    # Stitch op-failure detail into the feedback the next
                    # implementer call sees, so it understands WHY its
                    # last write/edit was rejected.
                    sub_feedback = review.feedback
                    if op_failure_block:
                        sub_feedback = (
                            f"{op_failure_block}\n\nREVIEWER:\n{review.feedback}"
                            if review.feedback else op_failure_block
                        )
                    sub_consec_fails += 1

                    # Track which check names failed THIS iteration; bump or
                    # reset their consecutive-fail counters.
                    failing_now = {r.name for r in check_results if not r.ok}
                    for r in check_results:
                        if not r.ok:
                            check_last_output[r.name] = (r.output or "")[:4000]
                    for name in list(check_consec_fails.keys()):
                        if name not in failing_now:
                            check_consec_fails.pop(name, None)
                    for name in failing_now:
                        check_consec_fails[name] = check_consec_fails.get(name, 0) + 1

                    # ─── Stage 1: self-heal the broken check ───
                    # Each check gets ONE repair attempt. After REPAIR_THRESHOLD
                    # consecutive fails, ask a planner-LLM to rewrite the check
                    # command itself (most often: missing --exclude-dir).
                    repair_candidates = [
                        name for name, n in check_consec_fails.items()
                        if n >= REPAIR_CHECK_THRESHOLD
                        and name not in check_repaired_once
                    ]
                    if repair_candidates:
                        from .check_repair import repair_quality_check
                        for cand_name in repair_candidates:
                            check_repaired_once.add(cand_name)
                            broken = next(
                                (c for c in sub_plan.quality_checks
                                 if c.name == cand_name),
                                None,
                            )
                            if broken is None:
                                continue
                            await _log(
                                store, task_id, "warn",
                                f"check '{cand_name}' failed "
                                f"{check_consec_fails.get(cand_name, '?')}× "
                                "in a row — attempting self-heal",
                            )
                            await _emit(progress, store, task_id, "log",
                                        {"msg": f"🔧 Repariere Check `{cand_name}` "
                                                "(Self-Heal)…"
                                                if lang == "de"
                                                else f"🔧 Repairing check `{cand_name}` "
                                                     "(self-heal)…"})
                            repaired = await repair_quality_check(
                                broken,
                                failure_output=check_last_output.get(cand_name, ""),
                                consecutive_failures=check_consec_fails.get(
                                    cand_name, 0,
                                ),
                                sub_plan_summary=sub_plan.summary,
                                lang=lang,
                                s=s,
                            )
                            if repaired is None:
                                await _log(
                                    store, task_id, "warn",
                                    f"check '{cand_name}': self-heal declined — "
                                    "will hard-abort if it fails again",
                                )
                                continue
                            new_checks = []
                            for c in sub_plan.quality_checks:
                                new_checks.append(
                                    repaired if c.name == cand_name else c
                                )
                            sub_plan = sub_plan.model_copy(
                                update={"quality_checks": new_checks},
                            )

                            # P3.5 — Persist the repaired check INTO the
                            # top-level plan stored at iteration 0 of the
                            # task row. Without this, a /resume after a
                            # crash or bot-restart re-loads the ORIGINAL
                            # broken check and the cascade has to rediscover
                            # the same repair (live-observed in Run #2 of
                            # task a4a98a — same self-heal happened twice).
                            try:
                                if plan.subtasks:
                                    new_subtasks = []
                                    for st in plan.subtasks:
                                        if st.name == subtask.name:
                                            st_new_checks = []
                                            for c in st.quality_checks:
                                                st_new_checks.append(
                                                    repaired if c.name == cand_name else c
                                                )
                                            new_subtasks.append(st.model_copy(
                                                update={"quality_checks": st_new_checks},
                                            ))
                                        else:
                                            new_subtasks.append(st)
                                    plan = plan.model_copy(update={"subtasks": new_subtasks})
                                else:
                                    new_top_checks = []
                                    for c in plan.quality_checks:
                                        new_top_checks.append(
                                            repaired if c.name == cand_name else c
                                        )
                                    plan = plan.model_copy(update={"quality_checks": new_top_checks})
                                await store.record_iteration(
                                    task_id, 0, implementer_output=plan.model_dump_json(),
                                )
                            except Exception as persist_err:
                                # Persistence is a nice-to-have — never let a
                                # DB hiccup block the in-memory repair.
                                log.debug(
                                    "check-repair persist into iter-0 failed: %s",
                                    persist_err,
                                )

                            # P3.5 — Cross-task learning: write the repair
                            # into RLM so future tasks' planner-recall can
                            # surface "we've seen this broken check shape
                            # before" hints. Tagged so BM25 lookup matches
                            # similar names + similar plan contexts.
                            try:
                                await remember_finding(
                                    f"cascade check-repair: '{cand_name}' was broken with "
                                    f"command `{broken.command[:300]}` — repaired to "
                                    f"`{repaired.command[:300]}`.",
                                    category="finding",
                                    importance="medium",
                                    tags=(
                                        f"cascade-bot-mcp,check-repair,"
                                        f"{cand_name.lower().replace(' ', '-')[:40]}"
                                    ),
                                    extra={"task_id": task_id, "subtask": subtask.name},
                                )
                            except Exception as rlm_err:
                                log.debug("check-repair RLM-save failed: %s", rlm_err)

                            check_consec_fails.pop(cand_name, None)
                            check_last_output.pop(cand_name, None)
                            await _log(
                                store, task_id, "info",
                                f"check '{cand_name}' repaired: "
                                f"{broken.command!r} → {repaired.command!r}",
                            )
                            await _emit(
                                progress, store, task_id, "log",
                                {"msg": f"✅ Check `{cand_name}` repariert + persistiert: "
                                        f"`{repaired.command[:120]}`"
                                        if lang == "de"
                                        else f"✅ Check `{cand_name}` repaired + persisted: "
                                             f"`{repaired.command[:120]}`"},
                            )

                    # ─── Stage 2: hard-abort if even after repair the check
                    # keeps failing. The threshold counts include any post-
                    # repair iterations (we explicitly cleared the counter
                    # for repaired checks above, so this only fires for
                    # checks the repair actually made worse or that we
                    # declined to repair).
                    broken_checks = [
                        name for name, n in check_consec_fails.items()
                        if n >= BROKEN_CHECK_THRESHOLD
                    ]
                    if broken_checks:
                        names_str = ", ".join(f"`{n}`" for n in broken_checks)
                        # P1.3: before hard-aborting, try ONE more replan
                        # with a hint that the check is hostile. Maybe the
                        # planner can drop it or replace with something
                        # achievable. Only hard-abort if replan-budget is
                        # already exhausted.
                        if sub_replans < s.cascade_replan_max:
                            hostile_hint = (
                                f"\n\nKRITISCH: Check(s) {names_str} sind "
                                f"trotz Self-Heal-Versuch {BROKEN_CHECK_THRESHOLD}× "
                                "in Folge gefailed. Überdenke ob dieser Check "
                                "überhaupt nötig ist — ggf. weglassen, durch "
                                "py_compile / einfache file-existence-Prüfung "
                                "ersetzen, oder das Acceptance-Criterion "
                                "lockern."
                            ) if lang == "de" else (
                                f"\n\nCRITICAL: check(s) {names_str} failed "
                                f"{BROKEN_CHECK_THRESHOLD}× in a row even "
                                "after self-heal. Reconsider whether the "
                                "check is needed — drop it, replace with "
                                "py_compile / a simple file-existence test, "
                                "or relax the acceptance criterion."
                            )
                            sub_feedback = (sub_feedback or "") + hostile_hint
                            sub_consec_fails = max(
                                sub_consec_fails,
                                s.cascade_replan_after_failures,
                            )
                            for n in broken_checks:
                                check_consec_fails.pop(n, None)
                            await _log(
                                store, task_id, "warn",
                                f"hostile check(s) {names_str}: forcing "
                                "sub-task replan with drop-hint instead of "
                                "hard-abort (replans_done="
                                f"{sub_replans}/{s.cascade_replan_max})",
                            )
                            await _emit(progress, store, task_id, "log",
                                        {"msg": (f"⚠️ Check(s) {names_str} "
                                                 "renitent — versuche Replan "
                                                 "mit Drop-Hinweis"
                                                 if lang == "de"
                                                 else f"⚠️ check(s) {names_str} "
                                                      "stubborn — trying replan "
                                                      "with drop-hint")})
                        else:
                            msg_de = (
                                f"❌ Stuck-Check abort: {names_str} ist "
                                f"{BROKEN_CHECK_THRESHOLD}× in Folge gefailed "
                                f"(auch nach Self-Heal + {sub_replans} Replan). "
                                "Vermutlich strukturell defekt. Sub-Task "
                                "abgebrochen damit der Run nicht weiter "
                                "Tokens verbrennt."
                            )
                            msg_en = (
                                f"❌ Stuck-check abort: {names_str} failed "
                                f"{BROKEN_CHECK_THRESHOLD}× in a row even "
                                f"after self-heal + {sub_replans} replan(s). "
                                "Likely structurally broken. Sub-task "
                                "aborted to stop burning tokens."
                            )
                            explain = msg_de if lang == "de" else msg_en
                            await _log(store, task_id, "error", explain)
                            await _emit(progress, store, task_id, "log",
                                        {"msg": explain})
                            all_subs_ok = False
                            last_sub_feedback = explain
                            sub_ok = False
                            break  # exits the sub_iter loop

                    # Sub-task local replan: if the sub-task has been stuck
                    # for `cascade_replan_after_failures` rounds AND we still
                    # have replan budget AND there's at least one sub-iter
                    # left to use the new plan → ask the planner to rewrite
                    # JUST THIS sub-task. Other sub-tasks stay untouched.
                    sub_iters_left = sub_iter_budget - sub_iter
                    if (
                        sub_consec_fails >= s.cascade_replan_after_failures
                        and sub_replans < s.cascade_replan_max
                        and sub_iters_left >= 1
                    ):
                        await _emit(progress, store, task_id, "replanning",
                                    {"after_iteration": cumulative_iter,
                                     "replans_done": sub_replans,
                                     "subtask": subtask.name})
                        replan_block = _build_replan_feedback(
                            sub_plan, await store.list_iterations(task_id)
                        )
                        try:
                            # P3.1: route through the multi-plan voter
                            # when enabled. Sub-task replans are exactly
                            # the spot where two competing rewrites help —
                            # one of them often dodges the broken-check
                            # the other inherits.
                            new_top = await _planner_or_multiplan(
                                task,
                                s=s,
                                attachments=attachments,
                                recall_context=recall,
                                repo_candidates_block=repo_candidates_block,
                                replan_feedback=(
                                    f"The sub-task '{subtask.name}' is stuck. "
                                    "Rewrite ONLY this sub-task's steps / files / "
                                    "acceptance / quality_checks. Keep the other "
                                    "sub-tasks unchanged. Failure history follows.\n\n"
                                    + replan_block
                                ),
                                external_context=external_context,
                                temperature=planner_temperature,
                                lang=lang,
                            )
                            # Prefer matching sub-task by name; else first
                            # sub-task; else use top-level fields.
                            replacement = None
                            if new_top.subtasks:
                                for st in new_top.subtasks:
                                    if st.name == subtask.name:
                                        replacement = st
                                        break
                                if replacement is None:
                                    replacement = new_top.subtasks[0]
                            if replacement is not None:
                                sub_plan = _Plan(
                                    summary=f"[{st_idx}/{len(subtasks)} · "
                                            f"{replacement.name}] {replacement.summary}",
                                    steps=replacement.steps,
                                    files_to_touch=replacement.files_to_touch,
                                    acceptance_criteria=replacement.acceptance_criteria,
                                    quality_checks=replacement.quality_checks,
                                    repo=plan.repo,
                                )
                            else:
                                sub_plan = _Plan(
                                    summary=f"[{st_idx} · {subtask.name}] {new_top.summary}",
                                    steps=new_top.steps,
                                    files_to_touch=new_top.files_to_touch,
                                    acceptance_criteria=new_top.acceptance_criteria,
                                    quality_checks=new_top.quality_checks,
                                    repo=plan.repo,
                                )
                            sub_plan = augment_quality_checks_for_python(sub_plan)

                            # P3.5 — Persist the new sub-task plan into the
                            # top-level plan stored at iter-0, so /resume
                            # after a crash continues with the IMPROVED
                            # plan instead of re-running the same
                            # discovery from the original broken one.
                            try:
                                if plan.subtasks and replacement is not None:
                                    new_subtasks = []
                                    for st in plan.subtasks:
                                        if st.name == subtask.name:
                                            new_subtasks.append(replacement)
                                        else:
                                            new_subtasks.append(st)
                                    plan = plan.model_copy(update={"subtasks": new_subtasks})
                                    await store.record_iteration(
                                        task_id, 0,
                                        implementer_output=plan.model_dump_json(),
                                    )
                            except Exception as persist_err:
                                log.debug(
                                    "subtask-replan persist into iter-0 failed: %s",
                                    persist_err,
                                )

                            sub_replans += 1
                            sub_consec_fails = 0
                            sub_feedback = None
                            # Also reset per-check counters: under the new
                            # sub-plan, check names may differ; old counts
                            # are stale.
                            check_consec_fails.clear()
                            check_repaired_once.clear()
                            check_last_output.clear()
                            await _emit(progress, store, task_id, "replanned",
                                        {"summary": sub_plan.summary,
                                         "checks": [c.name for c in sub_plan.quality_checks],
                                         "subtask": subtask.name})
                            await remember_decision(
                                f"Sub-task '{subtask.name}' replanned at iter "
                                f"{cumulative_iter}: '{task[:80]}'",
                                importance="medium",
                                tags=f"cascade-bot-mcp,subtask-replan,{subtask.name}",
                                extra={"task_id": task_id},
                            )
                        except Exception as e:
                            await _log(store, task_id, "warn",
                                       f"sub-task replan failed, continuing with old plan: {e}")
                if not sub_ok:
                    all_subs_ok = False
                    last_sub_feedback = sub_feedback or ""
                    # Continue to the next sub-task instead of abandoning
                    # the whole supervisor. A later sub-task may still
                    # succeed and add value — and the integration review
                    # gets a richer diff to judge. The all_subs_ok=False
                    # flag still prevents stamping the run as `done`.
                    await _log(store, task_id, "warn",
                               f"subtask {subtask.name} failed after {sub_iter_budget} iter — "
                               f"continuing with remaining sub-tasks.")

            # ---------- final integration review with auto-repair ----------
            # If the integration reviewer rejects, kick off a small fix-loop
            # that hands its feedback back to the implementer (treating the
            # whole plan as one virtual sub-task). Capped by INTEGRATION_REPAIR_MAX
            # so a hopeless run can't burn forever.
            INTEGRATION_REPAIR_MAX = max(2, s.cascade_max_iterations // 4)
            integration: ReviewResult | None = None
            full_diff = ws.diff_cumulative()
            if all_subs_ok:
                repair_attempts = 0
                while True:
                    await _emit(progress, store, task_id, "log",
                                {"msg": (
                                    f"integration-review (repair {repair_attempts})"
                                    if repair_attempts else
                                    "integration-review across all sub-tasks"
                                )})
                    try:
                        integration = await call_reviewer(
                            plan, full_diff, check_results=None,
                            external_context=external_context,
                            temperature=reviewer_temperature, lang=lang, task=task, s=s,
                        )
                    except Exception as e:
                        return await _fail(store, task_id, ws, "integration review", e, progress)
                    if integration.passed:
                        # P1.4: cumulative final quality gate across the
                        # whole plan. A later sub-task may have broken an
                        # earlier sub-task's invariants.
                        gate_ok, gate_failing = await _final_quality_gate(
                            plan=plan, ws=ws, store=store, task_id=task_id,
                            progress=progress, lang=lang,
                        )
                        if not gate_ok:
                            summary = (
                                "Final quality gate failed after integration: "
                                + ", ".join(gate_failing)
                            )
                            await store.update_task(
                                task_id, status="failed",
                                result_summary=summary, completed=True,
                            )
                            await _emit(progress, store, task_id, "failed",
                                        {"summary": summary})
                            await _try_reflect(
                                task=task, plan=plan,
                                iterations=cumulative_iter,
                                final_diff=full_diff, task_id=task_id,
                                store=store, s=s, lang=lang,
                            )
                            return CascadeResult(
                                task_id=task_id, status="failed",
                                iterations=cumulative_iter,
                                plan=plan, final_review=integration,
                                workspace_path=ws.root,
                                summary=summary, diff=full_diff,
                                changed_files=ws.changed_paths(),
                                error="final_quality_gate_failed",
                            )
                        summary = (
                            f"done via decomposition "
                            f"({len(subtasks)} sub-tasks, {cumulative_iter} iter"
                            + (f", +{repair_attempts} integration-repair" if repair_attempts else "")
                            + ")"
                        )
                        await store.update_task(task_id, status="done",
                                                result_summary=summary, completed=True)
                        await _emit(progress, store, task_id, "done", {"summary": summary})
                        # P3.3: post-run reflection for the decompose path
                        # (was previously only in the no-decompose done-path).
                        await _try_reflect(
                            task=task, plan=plan, iterations=cumulative_iter,
                            final_diff=full_diff, task_id=task_id,
                            store=store, s=s, lang=lang,
                        )
                        return CascadeResult(
                            task_id=task_id, status="done", iterations=cumulative_iter,
                            plan=plan, final_review=integration, workspace_path=ws.root,
                            summary=summary, diff=full_diff, changed_files=ws.changed_paths(),
                        )
                    last_sub_feedback = integration.feedback
                    if repair_attempts >= INTEGRATION_REPAIR_MAX:
                        break
                    repair_attempts += 1
                    cumulative_iter += 1
                    await _emit(progress, store, task_id, "implementing",
                                {"iteration": cumulative_iter,
                                 "subtask": "integration-repair"})
                    try:
                        impl = await call_implementer(
                            plan, workspace_files=ws.list_files(),
                            feedback=(
                                "Integration review failed. The user-visible diff "
                                "has been judged incomplete. Fix the gaps. "
                                f"Reviewer feedback:\n{integration.feedback}"
                            ),
                            iteration=cumulative_iter,
                            model=implementer_model,
                            provider=implementer_provider,
                            effort=s.cascade_implementer_effort or None,
                            temperature=implementer_temperature,
                            external_context=external_context, s=s,
                            file_contents=ws.read_files(
                                ws.candidate_context_files(plan.files_to_touch, limit=12)
                            ) if plan.files_to_touch else {},
                        )
                    except Exception as e:
                        return await _fail(store, task_id, ws,
                                           f"integration repair iter {cumulative_iter}", e, progress)
                    op_results = ws.apply_ops(impl.ops)
                    await _emit(progress, store, task_id, "implemented",
                                {"iteration": cumulative_iter, "ops": len(impl.ops),
                                 "failed": sum(1 for r in op_results if not r.ok),
                                 "subtask": "integration-repair"})
                    ws.commit_iteration(cumulative_iter)
                    full_diff = ws.diff_cumulative()
                    # loop: integration reviewer sees the new state next round

            summary = (f"decomposed run failed: "
                       f"{(last_sub_feedback or 'unknown')[:200]}")
            await store.update_task(task_id, status="failed",
                                    result_summary=summary, completed=True)
            await _emit(progress, store, task_id, "failed", {"summary": summary})
            return CascadeResult(
                task_id=task_id, status="failed", iterations=cumulative_iter,
                plan=plan, final_review=integration, workspace_path=ws.root,
                summary=summary, diff=full_diff, changed_files=ws.changed_paths(),
            )

        # Loop
        last_review: ReviewResult | None = None
        feedback: str | None = None
        consecutive_failures = 0
        replans_done = 0
        iter_n = start_iter
        for iter_n in range(start_iter, s.cascade_max_iterations + 1):
            await _check_cancel(cancel_event, store, task_id)
            await store.update_task(task_id, iteration=iter_n)

            # P3.6: implementer-stuck escalation in no-decompose path.
            # Same hook as the sub-task loop — healing flags it, we
            # bump consecutive_failures to force the next replan
            # trigger. Without this a stuck implementer just produces
            # the same diff each iter until cascade_max_iterations.
            if healing_state.implementer_stuck and replans_done < s.cascade_replan_max:
                await _log(
                    store, task_id, "warn",
                    "implementer-stuck flagged by healing — forcing replan",
                )
                consecutive_failures = max(
                    consecutive_failures,
                    s.cascade_replan_after_failures,
                )
                healing_state.implementer_stuck = False  # consumed

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
                    effort=s.cascade_implementer_effort or None,
                    temperature=implementer_temperature,
                    external_context=external_context,
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
                review: ReviewResult = await call_reviewer(
                    plan, diff, check_results=check_results,
                    external_context=external_context,
                    temperature=reviewer_temperature, lang=lang, task=task, s=s,
                )
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
                # P1.4: re-run plan-level quality_checks before stamping done.
                # In the no-decompose path the reviewer-pass already implies
                # the iter's checks passed, but a cumulative re-run is still
                # cheap insurance against accidental regressions.
                gate_ok, gate_failing = await _final_quality_gate(
                    plan=plan, ws=ws, store=store, task_id=task_id,
                    progress=progress, lang=lang,
                )
                if not gate_ok:
                    summary = (
                        f"Final quality gate failed after {iter_n} iter: "
                        + ", ".join(gate_failing)
                    )
                    await store.update_task(
                        task_id, status="failed",
                        result_summary=summary, completed=True,
                    )
                    await _emit(progress, store, task_id, "failed",
                                {"summary": summary})
                    await _try_reflect(
                        task=task, plan=plan, iterations=iter_n,
                        final_diff=ws.diff(), task_id=task_id,
                        store=store, s=s, lang=lang,
                    )
                    return CascadeResult(
                        task_id=task_id, status="failed", iterations=iter_n,
                        plan=plan, final_review=review, workspace_path=ws.root,
                        summary=summary, diff=ws.diff(),
                        changed_files=ws.changed_paths(),
                        error="final_quality_gate_failed",
                    )
                summary = f"done after {iter_n} iteration(s)"
                await store.update_task(
                    task_id,
                    status="done",
                    result_summary=summary,
                    completed=True,
                )
                await _emit(progress, store, task_id, "done", {"summary": summary})

                # Always record successful runs in memory — workspace + files
                # are the durable artifacts that future runs may want to reuse.
                changed = ws.changed_paths()
                await remember_finding(
                    f"Task done: '{task[:120]}' → workspace={ws.root}, "
                    f"iters={iter_n}, files_changed={len(changed)}, "
                    f"plan_summary={(plan.summary or '')[:200]}",
                    category="finding",
                    importance="medium" if review.severity == "low" else "high",
                    tags=f"cascade-bot-mcp,task,{source}",
                    extra={"task_id": task_id, "review_severity": review.severity},
                )

                # Self-reflection (now via shared helper used by all
                # done- and failed-paths).
                await _try_reflect(
                    task=task, plan=plan, iterations=iter_n,
                    final_diff=ws.diff_cumulative(max_bytes=50_000),
                    task_id=task_id, store=store, s=s, lang=lang,
                )

                # Auto-skill suggestion (best-effort, non-blocking on failure).
                if s.cascade_auto_skill_suggest:
                    try:
                        recent = await store.list_tasks(limit=10, status="done")
                        # exclude the current task to avoid double-counting
                        recent = [t for t in recent if t.id != task_id][:8]
                        existing = await store.list_skills()
                        cur_task = await store.get_task(task_id)
                        # Cooldown: when did we last *make* a suggestion?
                        last_sug_ts = max(
                            (sk.get("created_at") or 0 for sk in existing),
                            default=0,
                        )
                        sug = await maybe_suggest_skill(
                            current_task=cur_task,
                            recent_tasks=recent,
                            existing_skills=existing,
                            s=s,
                            cooldown_s=s.cascade_skill_suggest_cooldown_s,
                            last_suggested_at=last_sug_ts or None,
                        )
                        if sug:
                            await store.record_skill_suggestion(
                                task_id, sug.model_dump(), chat_id=None
                            )
                            await _emit(
                                progress, store, task_id, "skill_suggested",
                                {
                                    "name": sug.name,
                                    "description": sug.description,
                                    "task_template": sug.task_template,
                                    "placeholders": sug.placeholders,
                                    "rationale": sug.rationale,
                                },
                            )
                    except Exception as e:
                        await _log(store, task_id, "warn", f"skill suggest failed: {e}")

                return CascadeResult(
                    task_id=task_id,
                    status="done",
                    iterations=iter_n,
                    plan=plan,
                    final_review=review,
                    workspace_path=ws.root,
                    summary=summary,
                    diff=diff,
                    changed_files=ws.changed_paths(),
                )

            # not passed → next iteration. Stitch op-failure detail INTO
            # feedback so the implementer's next call sees WHY its writes
            # were rejected (path/syntax/stub). Without this it loops on
            # an "empty diff" complaint and degrades to no-ops.
            if failed_ops:
                op_block = "OP FAILURES (must address):\n" + "\n".join(
                    f"  - {r.op} {r.path}: {r.detail}" for r in failed_ops
                )
                feedback = (
                    f"{op_block}\n\nREVIEWER:\n{review.feedback}"
                    if review.feedback else op_block
                )
            else:
                feedback = review.feedback
            consecutive_failures += 1
            await _emit(
                progress,
                store,
                task_id,
                "iteration_failed",
                {"iteration": iter_n, "feedback": feedback},
            )

            # Stagnation detection: if the LAST two iterations produced the
            # exact same reviewer feedback (modulo whitespace), we're spinning.
            # Force a replan on the spot, ignoring `cascade_replan_after_failures`,
            # so the planner has a chance to rewrite the broken instruction
            # before we burn another implementer call.
            stagnation = False
            try:
                hist = await store.list_iterations(task_id)
                fbs = [
                    (it.reviewer_feedback or "").strip()
                    for it in hist
                    if it.n > 0 and it.reviewer_pass is False
                ]
                if (
                    len(fbs) >= 2
                    and fbs[-1] == fbs[-2]
                    and fbs[-1]  # don't trigger on consecutive empty feedbacks
                ):
                    stagnation = True
            except Exception:
                stagnation = False

            # Hard escape: stagnation detected AND replan budget exhausted →
            # there's no realistic path forward (we'd just keep producing the
            # same broken iteration). End the run with `failed` instead of
            # looping until cascade_max_iterations (=999 by default).
            iters_left = s.cascade_max_iterations - iter_n
            if stagnation and replans_done >= s.cascade_replan_max:
                await _log(
                    store, task_id, "error",
                    f"stagnation + replan budget exhausted (replans={replans_done}/"
                    f"{s.cascade_replan_max}) — aborting to prevent infinite loop.",
                )
                summary = (
                    "Run abgebrochen: Stagnation erkannt und Replan-Budget "
                    "erschöpft. Letztes Review-Feedback:\n"
                    + (feedback or "(leer)")[:600]
                ) if lang == "de" else (
                    "Aborted: stagnation detected and replan budget exhausted. "
                    "Last reviewer feedback:\n" + (feedback or "(empty)")[:600]
                )
                await store.update_task(
                    task_id, status="failed",
                    result_summary=summary, completed=True,
                )
                await _emit(progress, store, task_id, "failed",
                            {"reason": "stagnation_replan_exhausted",
                             "feedback": feedback})
                # P3.3: postmortem on stagnation aborts is high-value —
                # this is the cascade telling us something about how
                # plan and reviewer-feedback got locked together.
                await _try_reflect(
                    task=task, plan=plan, iterations=iter_n,
                    final_diff=ws.diff(), task_id=task_id,
                    store=store, s=s, lang=lang,
                )
                return CascadeResult(
                    task_id=task_id,
                    status="failed",
                    iterations=iter_n,
                    plan=plan,
                    final_review=review,
                    workspace_path=ws.root,
                    summary=summary,
                    diff=ws.diff(),
                    changed_files=ws.changed_paths(),
                    error="stagnation_replan_exhausted",
                )

            # Re-plan trigger: if we've been stuck for N consecutive iterations
            # AND we still have replan budget AND there are more iterations left
            # to actually use the new plan, ask the planner to rewrite the plan
            # (especially the quality_checks). Solves the loop deadlock when
            # the plan itself is wrong (e.g. python vs python3).
            # The stagnation override skips the consecutive-failures threshold.
            if (
                (consecutive_failures >= s.cascade_replan_after_failures or stagnation)
                and replans_done < s.cascade_replan_max
                and iters_left >= 1
            ):
                if stagnation:
                    await _log(
                        store, task_id, "warn",
                        "stagnation detected: identical reviewer feedback in "
                        "last 2 iterations — forcing replan now.",
                    )
                replan_block = _build_replan_feedback(plan, await store.list_iterations(task_id))
                await _emit(progress, store, task_id, "replanning",
                            {"after_iteration": iter_n, "replans_done": replans_done})
                try:
                    # P3.1: same multi-plan dispatch as the initial planner
                    # call, so global replans benefit too.
                    new_plan = await _planner_or_multiplan(
                        task,
                        s=s,
                        attachments=attachments,
                        recall_context=recall,
                        repo_candidates_block=repo_candidates_block,
                        replan_feedback=replan_block,
                        external_context=external_context,
                        temperature=planner_temperature,
                        lang=lang,
                    )
                    plan = augment_quality_checks_for_python(new_plan)
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
                    await remember_decision(
                        f"Cascade replanned after iter {iter_n}: '{task[:80]}'. "
                        f"new plan: {plan.summary[:150]}",
                        importance="medium",
                        tags="cascade-bot-mcp,replan",
                        extra={"task_id": task_id},
                    )
                except Exception as e:
                    await _log(store, task_id, "warn", f"replan failed, continuing with old plan: {e}")

        # Max iterations exhausted
        summary = f"failed after {s.cascade_max_iterations} iterations"
        await store.update_task(
            task_id, status="failed", result_summary=summary, completed=True
        )
        await remember_finding(
            f"Task FAILED after max iters: '{task[:120]}'. "
            f"last review: {(last_review.feedback if last_review else '—')[:200]}",
            category="finding", importance="high",
            tags=f"cascade-bot-mcp,task,failure,{source}",
            extra={"task_id": task_id},
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
            changed_files=ws.changed_paths(),
        )

    finally:
        # Stop the healing monitor cleanly on every exit path (return, raise,
        # cancellation). Created lazily inside the try-block; may not exist
        # if we crashed before reaching it.
        try:
            mon = locals().get("healing_monitor")
            if mon is not None:
                await mon.__aexit__(None, None, None)
        except Exception:
            pass
        # Reset the wait-notifier contextvar so leftover callbacks from a
        # previous run can't fire into a closed store on the next call.
        try:
            tok = locals().get("_wait_token")
            if tok is not None:
                from .rate_limit import WAIT_NOTIFIER
                WAIT_NOTIFIER.reset(tok)
        except Exception:
            pass
        if own_store:
            await store.close()


# ---------- helpers ----------


def augment_quality_checks_for_python(plan: Plan) -> Plan:
    """If the plan touches any .py files, add `python3 -m py_compile` and
    (when ruff is available system-wide) `ruff check` to its quality_checks
    — but only if equivalent checks aren't already there.

    The Planner often forgets these obvious checks, so the supervisor
    silently appends them. This raises the bar for "pass=true" without
    forcing the planner to think about it every time.
    """
    py_files = [
        p for p in (plan.files_to_touch or [])
        if p.lower().endswith(".py")
    ]
    if not py_files:
        return plan
    existing_cmds = " ".join(
        (c.command or "") for c in (plan.quality_checks or [])
    ).lower()
    additions = []
    if "py_compile" not in existing_cmds:
        # Quote each file so spaces in paths are safe.
        files_arg = " ".join(f"'{p}'" for p in py_files)
        from .workspace import QualityCheck
        additions.append(QualityCheck(
            name="py-compile",
            command=f"python3 -m py_compile {files_arg}",
            timeout_s=30,
        ))
    if "ruff" not in existing_cmds:
        # Only add ruff if it's installed; otherwise the check would
        # fail with a confusing "ruff: command not found" instead of
        # actually catching style issues.
        import shutil as _sh
        if _sh.which("ruff"):
            from .workspace import QualityCheck
            files_arg = " ".join(f"'{p}'" for p in py_files)
            additions.append(QualityCheck(
                name="ruff",
                command=f"ruff check {files_arg}",
                timeout_s=30,
            ))
    if not additions:
        return plan
    new_qcs = list(plan.quality_checks or []) + additions
    return plan.model_copy(update={"quality_checks": new_qcs})


def _build_replan_feedback(prev_plan: Plan, iter_history: list) -> str:
    """Render a compact summary of the previous plan + iteration failures
    so the planner can produce a corrected plan."""
    lines = [
        "PREVIOUS PLAN (now superseded):",
        f"  summary: {prev_plan.summary}",
        f"  steps: {prev_plan.steps}",
        f"  files_to_touch: {prev_plan.files_to_touch}",
        f"  acceptance_criteria: {prev_plan.acceptance_criteria}",
        "  quality_checks:",
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
    payload_str = json.dumps(payload, default=str)[:300]
    # Mirror cascade events into the standard logger too — that way
    # `journalctl -u cascade-bot -f` shows the actual phase transitions
    # instead of just httpx HTTP-noise.
    log.info("[%s] %s %s", task_id[:6], event, payload_str)
    await store.log(task_id, "info", f"{event}: {payload_str}")


async def _log(store: Store, task_id: str, level, message: str) -> None:
    await store.log(task_id, level, message)


async def _check_cancel(ev: asyncio.Event, store: Store, task_id: str) -> None:
    if ev.is_set():
        await store.update_task(task_id, status="cancelled", completed=True)
        await store.log(task_id, "warn", "cancelled by request")
        raise asyncio.CancelledError()


async def _try_reflect(
    *,
    task: str,
    plan: Plan | None,
    iterations: int,
    final_diff: str,
    task_id: str,
    store: Store,
    s: Settings,
    lang: str = "de",
) -> None:
    """Best-effort post-run self-reflection. Used by both done- and
    failed-paths so we capture lessons-learned regardless of outcome.

    Failures are arguably MORE valuable to reflect on — they're where
    the planner / implementer / reviewer disagreement was sharpest.
    Catches all exceptions so a flaky reflect-LLM never breaks the
    main run's terminal status update.
    """
    if plan is None:
        return
    try:
        from .reflect import persist_lessons, reflect_on_run
        lessons = await reflect_on_run(
            task=task, plan=plan, iterations=iterations,
            final_diff=(final_diff or "")[:50_000],
            s=s, lang=lang,
        )
        if lessons:
            await persist_lessons(
                lessons, task_id=task_id, task_text=task,
            )
            await _log(
                store, task_id, "info",
                f"lessons-learned saved: {lessons[:200]}",
            )
    except Exception as e:
        log.debug("reflect_on_run failed: %s", e)


async def _planner_or_multiplan(
    task: str,
    *,
    s: Settings,
    attachments=None,
    recall_context=None,
    repo_candidates_block=None,
    replan_feedback: str | None = None,
    external_context: str | None = None,
    temperature: float | None = None,
    lang: str = "en",
) -> Plan:
    """Single planner call → call_planner. Two-plan vote when
    `cascade_multiplan_enabled` is set → call_planner_multi.

    Used for both the initial plan and replans (sub-task and global).
    Centralising the dispatch means sub-task replans pick up multi-plan
    voting automatically, instead of always using the single-shot path.
    """
    if getattr(s, "cascade_multiplan_enabled", False):
        from .multiplan import call_planner_multi
        plan, _reason = await call_planner_multi(
            task,
            attachments=attachments,
            recall_context=recall_context,
            repo_candidates_block=repo_candidates_block,
            replan_feedback=replan_feedback,
            external_context=external_context,
            base_temperature=temperature,
            lang=lang,
            s=s,
        )
        return plan
    return await call_planner(
        task,
        attachments=attachments,
        recall_context=recall_context,
        repo_candidates_block=repo_candidates_block,
        replan_feedback=replan_feedback,
        external_context=external_context,
        temperature=temperature,
        lang=lang,
        s=s,
    )


async def _final_quality_gate(
    *,
    plan: Plan,
    ws: Workspace,
    store: Store,
    task_id: str,
    progress: ProgressCallback,
    lang: str = "de",
) -> tuple[bool, list[str]]:
    """Re-run all top-level plan.quality_checks one last time before stamping
    a task as `done`. Catches the case where a later sub-task / integration
    repair broke an earlier sub-task's invariant.

    Returns (all_pass, failing_check_names). On all_pass=True the caller may
    proceed to status='done'; on False it must mark status='failed' with a
    clear message naming the offending check(s).
    """
    if not plan.quality_checks:
        return True, []
    failing: list[str] = []
    for chk in plan.quality_checks:
        try:
            res = await ws.run_check(chk)
        except Exception as e:
            from .workspace import CheckResult as _CR
            res = _CR(chk.name, False, -3, f"runner-error: {e}", 0.0)
        if not res.ok:
            failing.append(chk.name)
    if failing:
        names_str = ", ".join(f"`{n}`" for n in failing)
        msg = (
            f"❌ Final quality gate: {names_str} fehlgeschlagen — Task "
            "wird NICHT als `done` markiert."
            if lang == "de"
            else f"❌ Final quality gate: {names_str} failed — task "
                 "will NOT be marked `done`."
        )
        await _log(store, task_id, "error", msg)
        await _emit(progress, store, task_id, "log", {"msg": msg})
    return (not failing), failing


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
