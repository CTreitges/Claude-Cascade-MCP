"""Verify the replan trigger: when N consecutive iterations fail, the planner
is invoked again with feedback so it can rewrite the plan."""

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
    s = await Store.open(tmp_path / "core.db")
    yield s
    await s.close()


@pytest.fixture
def s(tmp_path: Path) -> Settings:
    return Settings(
        cascade_home=tmp_path / "ws-home",
        cascade_max_iterations=5,
        cascade_replan_after_failures=2,
        cascade_replan_max=1,
        cascade_db_path=tmp_path / "core.db",
        cascade_implementer_provider="ollama",
        cascade_implementer_model="qwen3-coder:480b",
    )


async def test_replan_fires_after_two_failures(monkeypatch, store, s):
    """Bad plan with wrong check fails 2x → replan with correct check → pass."""
    bad_plan = Plan(
        summary="bad",
        steps=[],
        files_to_touch=["x.txt"],
        acceptance_criteria=[],
        quality_checks=[QualityCheck(name="bad", command="false", timeout_s=5)],
    )
    good_plan = Plan(
        summary="fixed",
        steps=[],
        files_to_touch=["x.txt"],
        acceptance_criteria=[],
        quality_checks=[QualityCheck(name="good", command="true", timeout_s=5)],
    )

    plans_iter = iter([bad_plan, good_plan])
    plan_calls = {"n": 0}

    async def fake_planner(*_args, replan_feedback=None, **_kw):
        plan_calls["n"] += 1
        if plan_calls["n"] == 1:
            assert replan_feedback is None
        else:
            # On the replan call, we MUST receive the failure history.
            assert replan_feedback is not None
            assert "bad" in replan_feedback  # name of failing check is in feedback
        return next(plans_iter)

    impl = ImplementerOutput(ops=[FileOp(op="write", path="x.txt", content="hi")])

    async def fake_implementer(*_a, **_kw):
        return impl

    async def fake_reviewer(*_a, **_kw):
        # Reviewer always says pass; the failed check is the only thing
        # holding the loop back. This isolates the replan trigger to the check.
        return ReviewResult(passed=True, feedback="")

    monkeypatch.setattr("cascade.core.call_planner", fake_planner)
    monkeypatch.setattr("cascade.core.call_implementer", fake_implementer)
    monkeypatch.setattr("cascade.core.call_reviewer", fake_reviewer)

    result = await run_cascade(task="x", store=store, s=s)
    assert result.status == "done"
    assert plan_calls["n"] == 2  # original + 1 replan
    # iter 1 failed, iter 2 failed → replan → iter 3 passes
    assert result.iterations == 3


async def test_replan_max_caps_calls(monkeypatch, store, s):
    """Even if we keep failing, we replan at most cascade_replan_max times."""
    s2 = s.model_copy(update={"cascade_replan_max": 1, "cascade_max_iterations": 5})

    bad_plan = Plan(
        summary="bad",
        steps=[],
        files_to_touch=["x.txt"],
        acceptance_criteria=[],
        quality_checks=[QualityCheck(name="always-fails", command="false", timeout_s=5)],
    )
    plan_calls = {"n": 0}

    async def fake_planner(*_a, **_kw):
        plan_calls["n"] += 1
        return bad_plan

    async def fake_implementer(*_a, **_kw):
        return ImplementerOutput(ops=[FileOp(op="write", path="x.txt", content="hi")])

    async def fake_reviewer(*_a, **_kw):
        return ReviewResult(passed=True, feedback="")

    monkeypatch.setattr("cascade.core.call_planner", fake_planner)
    monkeypatch.setattr("cascade.core.call_implementer", fake_implementer)
    monkeypatch.setattr("cascade.core.call_reviewer", fake_reviewer)

    result = await run_cascade(task="x", store=store, s=s2)
    assert result.status == "failed"
    # original plan + 1 replan = 2 planner calls (not more)
    assert plan_calls["n"] == 2


async def test_no_replan_when_passing(monkeypatch, store, s):
    """Happy path: pass on iter 1 → planner called exactly once."""
    plan = Plan(
        summary="ok",
        steps=[],
        files_to_touch=[],
        acceptance_criteria=[],
        quality_checks=[QualityCheck(name="ok", command="true", timeout_s=5)],
    )
    plan_calls = {"n": 0}

    async def fake_planner(*_a, **_kw):
        plan_calls["n"] += 1
        return plan

    async def fake_implementer(*_a, **_kw):
        return ImplementerOutput(ops=[])

    async def fake_reviewer(*_a, **_kw):
        return ReviewResult(passed=True, feedback="")

    monkeypatch.setattr("cascade.core.call_planner", fake_planner)
    monkeypatch.setattr("cascade.core.call_implementer", fake_implementer)
    monkeypatch.setattr("cascade.core.call_reviewer", fake_reviewer)

    result = await run_cascade(task="x", store=store, s=s)
    assert result.status == "done"
    assert plan_calls["n"] == 1
