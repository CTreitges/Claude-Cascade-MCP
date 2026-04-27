"""Targeted tests for the phase 1+2+3 reliability features added 2026-04-27.

Each test exercises one specific code path in cascade/core.py with mocked
agents so we don't need a real LLM. Live observation already confirmed
several of these paths work; this file is the regression net.

Covered:
  P1.1 — plan validation (empty plan → retry → fail)
  P1.2 — empty-ops loop-breaker
  P1.4 — final quality gate (post-done re-run all checks)
  P3.5 — self-heal repair persisted into iter-0 plan
  P3.6 — implementer-stuck auto-replan via healing flag
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cascade.agents.implementer import ImplementerOutput
from cascade.agents.planner import Plan
from cascade.agents.reviewer import ReviewResult
from cascade.config import Settings
from cascade.core import run_cascade
from cascade.store import Store
from cascade.workspace import FileOp, QualityCheck


@pytest.fixture
async def store(tmp_path: Path) -> Store:
    s = await Store.open(tmp_path / "phase.db")
    yield s
    await s.close()


@pytest.fixture
def s(tmp_path: Path) -> Settings:
    return Settings(
        cascade_home=tmp_path / "ws-home",
        cascade_max_iterations=4,
        cascade_replan_max=1,
        cascade_replan_after_failures=2,
        cascade_db_path=tmp_path / "phase.db",
        cascade_implementer_provider="ollama",
        cascade_implementer_model="qwen3-coder:480b",
        cascade_auto_skill_suggest=False,  # don't trigger skill flow in tests
    )


def _patch_planner(monkeypatch, plans):
    """Either a single Plan or a list of plans (for replan scenarios)."""
    if isinstance(plans, Plan):
        plans = [plans]
    iterator = iter(plans)
    last = plans[-1]

    async def fake(_task, **_kw):
        try:
            return next(iterator)
        except StopIteration:
            return last

    monkeypatch.setattr("cascade.core.call_planner", fake)


def _patch_implementer(monkeypatch, outputs):
    iterator = iter(outputs)

    async def fake(_plan, **_kw):
        try:
            return next(iterator)
        except StopIteration:
            return ImplementerOutput(ops=[])

    monkeypatch.setattr("cascade.core.call_implementer", fake)


def _patch_reviewer(monkeypatch, results):
    iterator = iter(results)

    async def fake(_plan, _diff, **_kw):
        try:
            return next(iterator)
        except StopIteration:
            return ReviewResult(passed=True, feedback="")

    monkeypatch.setattr("cascade.core.call_reviewer", fake)


# ─── P1.1: empty-plan validation ────────────────────────────────────


async def test_p11_empty_plan_retried_then_failed(monkeypatch, store, s):
    """First planner call returns an empty plan → cascade retries once →
    second call also empty → status=failed (no implementer iter spent)."""
    empty = Plan(summary="x", steps=[], files_to_touch=[],
                 acceptance_criteria=[])
    _patch_planner(monkeypatch, [empty, empty])

    # Implementer / reviewer should NEVER be called — the run dies on
    # plan validation before workspace setup.
    impl_calls = {"n": 0}

    async def fake_impl(*_a, **_kw):
        impl_calls["n"] += 1
        return ImplementerOutput(ops=[])

    monkeypatch.setattr("cascade.core.call_implementer", fake_impl)

    result = await run_cascade(task="anything", store=store, s=s)
    assert result.status == "failed"
    assert "empty plan" in (result.error or "").lower()
    assert impl_calls["n"] == 0


async def test_p11_actionable_plan_passes_validation(monkeypatch, store, s):
    """A plan with only `acceptance_criteria` populated still passes
    validation (no implementer iter triggered, no spurious failure)."""
    plan = Plan(
        summary="ok", steps=[], files_to_touch=[],
        acceptance_criteria=["something must be true"],
    )
    _patch_planner(monkeypatch, plan)
    _patch_implementer(monkeypatch, [ImplementerOutput(ops=[])])
    _patch_reviewer(monkeypatch, [ReviewResult(passed=True, feedback="")])

    result = await run_cascade(task="x", store=store, s=s)
    assert result.status == "done"


# ─── P1.2: empty-ops loop-breaker ───────────────────────────────────


async def test_p12_empty_ops_two_in_a_row_forces_replan(monkeypatch, store, s):
    """Two consecutive empty-ops impl outputs → cascade forces a replan
    instead of running another identical iter. Verified by counting
    planner calls (1 initial + 1 forced replan = 2)."""
    plan = Plan(
        summary="t", steps=["a"], files_to_touch=["a.py"],
        acceptance_criteria=["touched"],
        subtasks=[],
    )
    plan_calls = {"n": 0}

    async def fake_planner(*_a, **_kw):
        plan_calls["n"] += 1
        return plan

    monkeypatch.setattr("cascade.core.call_planner", fake_planner)
    _patch_implementer(monkeypatch, [
        ImplementerOutput(ops=[], rationale="cannot proceed"),
        ImplementerOutput(ops=[], rationale="still cannot"),
        ImplementerOutput(ops=[FileOp(op="write", path="a.py", content="x=1\n")]),
    ])
    _patch_reviewer(monkeypatch, [
        ReviewResult(passed=False, feedback="empty diff"),
        ReviewResult(passed=False, feedback="still empty"),
        ReviewResult(passed=True, feedback=""),
    ])

    result = await run_cascade(task="t", store=store, s=s)
    # Planner should have been called at least twice (initial + forced replan).
    assert plan_calls["n"] >= 2, f"expected forced replan, planner_calls={plan_calls['n']}"


# ─── P1.4: final quality gate ───────────────────────────────────────


async def test_p14_final_quality_gate_blocks_done_when_check_fails(
    monkeypatch, store, s, tmp_path,
):
    """Reviewer says pass=true, but a plan-level quality_check still
    fails on the final gate run → status=failed (NOT done)."""
    # A check that always fails — `false` exits 1.
    plan = Plan(
        summary="gated", steps=["s1"], files_to_touch=[],
        acceptance_criteria=[],
        quality_checks=[
            QualityCheck(name="always-fails", command="false", timeout_s=2),
        ],
    )
    # Reviewer (mocked) ignores the failing check and says pass=true —
    # the final gate must catch this regression.
    _patch_planner(monkeypatch, plan)
    _patch_implementer(monkeypatch, [
        ImplementerOutput(ops=[FileOp(op="write", path="x.txt", content="hi\n")]),
    ])
    _patch_reviewer(monkeypatch, [ReviewResult(passed=True, feedback="lgtm")])

    result = await run_cascade(task="x", store=store, s=s)
    # Without the gate this would be "done"; the gate flips it.
    assert result.status == "failed"
    assert (result.error or "").startswith("final_quality_gate") or \
           "always-fails" in (result.summary or "")


# ─── P3.5: self-heal-repair persistence into iter-0 plan ────────────


async def test_p35_repair_persists_into_iter0_plan(
    monkeypatch, store, tmp_path,
):
    """When repair_quality_check rewrites a sub-task check, the new
    command must land in the iter-0 plan stored in DB so /resume
    after a crash picks up the repaired version.

    Needs enough sub-iter budget that after the regular replan exhausts,
    we still get 3 consecutive same-check fails to trip the repair
    threshold. With 1 sub-task and cascade_max_iterations=10, sub_iter
    budget is 10, plenty of room.
    """
    s = Settings(
        cascade_home=tmp_path / "ws-home",
        cascade_max_iterations=10,
        cascade_replan_max=1,
        cascade_replan_after_failures=2,
        cascade_db_path=tmp_path / "phase.db",
        cascade_implementer_provider="ollama",
        cascade_implementer_model="qwen3-coder:480b",
        cascade_auto_skill_suggest=False,
    )
    from cascade.agents.planner import SubTask

    # Sub-task plan with a check that the implementer can never satisfy
    # (the check itself is buggy — scans .venv/).
    bad_check = QualityCheck(
        name="busted-check",
        command="false",  # always fails
        timeout_s=2,
    )
    repaired_check = QualityCheck(
        name="busted-check",
        command="true",   # always passes
        timeout_s=2,
    )
    plan = Plan(
        summary="decompose",
        steps=[],
        files_to_touch=[],
        acceptance_criteria=[],
        subtasks=[
            SubTask(
                name="only-sub",
                summary="dummy",
                steps=["s"],
                files_to_touch=[],
                acceptance_criteria=["a"],
                quality_checks=[bad_check],
            ),
        ],
    )
    _patch_planner(monkeypatch, plan)
    # Implementer always writes a benign file.
    _patch_implementer(monkeypatch, [
        ImplementerOutput(ops=[FileOp(op="write", path="x.txt", content=str(i))])
        for i in range(15)
    ])
    _patch_reviewer(monkeypatch, [
        ReviewResult(passed=False, feedback="check failed") for _ in range(15)
    ])

    # Force the repair to return a working check.
    repair_calls = {"n": 0}

    async def fake_repair(check, **_kw):
        repair_calls["n"] += 1
        return repaired_check

    monkeypatch.setattr(
        "cascade.check_repair.repair_quality_check", fake_repair,
    )

    await run_cascade(task="x", store=store, s=s)
    # Repair must have been called (after 3 consecutive same-check fails).
    assert repair_calls["n"] >= 1, "repair_quality_check should have fired"

    # Iter-0 plan in the DB now carries the repaired check command.
    iters = await store.list_iterations((await store.latest_task()).id)
    iter0 = next(i for i in iters if i.n == 0)
    saved = Plan.model_validate_json(iter0.implementer_output)
    saved_check = saved.subtasks[0].quality_checks[0]
    assert saved_check.command == "true", \
        f"expected repaired command in iter-0, got {saved_check.command!r}"


# ─── P3.6: implementer-stuck auto-replan via healing flag ───────────


async def test_p36_implementer_stuck_flag_forces_replan(
    monkeypatch, store, s,
):
    """When healing flips `implementer_stuck=True`, the cascade loop
    must escalate consecutive_failures so the next iter triggers a
    replan, instead of letting the same diff echo forever."""
    plan = Plan(
        summary="stuck-test", steps=["s1"], files_to_touch=["x.py"],
        acceptance_criteria=["done"],
    )
    plan_calls = {"n": 0}

    async def fake_planner(*_a, **_kw):
        plan_calls["n"] += 1
        return plan

    monkeypatch.setattr("cascade.core.call_planner", fake_planner)

    # Implementer outputs identical ops every iter — exactly the case
    # healing detects via 3-in-a-row identical hashes.
    same = ImplementerOutput(ops=[
        FileOp(op="write", path="x.py", content="same\n"),
    ])
    _patch_implementer(monkeypatch, [same] * 6)
    _patch_reviewer(monkeypatch, [
        ReviewResult(passed=False, feedback="not ok") for _ in range(6)
    ])

    # Force the stuck flag immediately so we don't have to wait for the
    # healing tick — the loop's read of healing_state.implementer_stuck
    # is the contract under test.
    import cascade.core as core_mod
    orig_run_cascade = core_mod.run_cascade

    async def wrapping_run(*args, **kwargs):
        # Patch HealingState's default to start with the flag set.
        # Less invasive: monkeypatch HealingState.__init__ post-hoc.
        return await orig_run_cascade(*args, **kwargs)

    # Patch the HealingState ctor so implementer_stuck=True from the start.
    from cascade.healing import HealingState
    orig_init = HealingState.__init__

    def patched_init(self):
        orig_init(self)
        self.implementer_stuck = True

    monkeypatch.setattr(HealingState, "__init__", patched_init)

    await run_cascade(task="t", store=store, s=s)
    # Replan should fire on the first iter because the flag is True.
    assert plan_calls["n"] >= 2, \
        f"implementer_stuck flag should have forced a replan; planner_calls={plan_calls['n']}"
