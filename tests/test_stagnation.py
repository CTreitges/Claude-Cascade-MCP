"""Tests for the stagnation-detection + hard-escape paths in core.py.

Two situations covered:

  1. **Stagnation triggers immediate replan** — when the reviewer returns
     identical feedback in two consecutive iterations, run_cascade must
     re-call the planner BEFORE `cascade_replan_after_failures` would
     normally fire. Otherwise the implementer keeps producing the same
     broken output until the threshold is reached.

  2. **Hard escape on exhausted replan budget** — once `replan_max` is
     consumed AND stagnation is still detected, the run must end with
     status='failed' instead of looping until `cascade_max_iterations`
     (=999 in default config). Without this, a misconfigured plan
     could burn 999 LLM calls before stopping.
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
    s = await Store.open(tmp_path / "stag.db")
    yield s
    await s.close()


@pytest.fixture
def base_settings(tmp_path: Path) -> Settings:
    return Settings(
        cascade_home=tmp_path / "ws",
        cascade_max_iterations=999,
        cascade_replan_after_failures=10,  # high — only stagnation can trigger
        cascade_replan_max=2,
        cascade_db_path=tmp_path / "stag.db",
    )


async def test_stagnation_triggers_replan_before_threshold(
    monkeypatch, store, base_settings,
):
    """Identical feedback two iterations in a row must replan EVEN THOUGH
    `cascade_replan_after_failures` (10) hasn't been reached yet."""
    bad = Plan(
        summary="bad",
        steps=[],
        files_to_touch=["x.txt"],
        acceptance_criteria=[],
        quality_checks=[QualityCheck(name="ok", command="true", timeout_s=5)],
    )
    good = Plan(
        summary="good",
        steps=[],
        files_to_touch=["x.txt"],
        acceptance_criteria=[],
        quality_checks=[QualityCheck(name="ok", command="true", timeout_s=5)],
    )
    plans = iter([bad, good])
    planner_calls = {"n": 0}

    async def fake_planner(*_a, **_kw):
        planner_calls["n"] += 1
        return next(plans)

    impl = ImplementerOutput(ops=[FileOp(op="write", path="x.txt", content="x")])

    async def fake_implementer(*_a, **_kw):
        return impl

    review_calls = {"n": 0}

    async def fake_reviewer(*_a, **_kw):
        review_calls["n"] += 1
        # iter 1, iter 2 → fail with IDENTICAL feedback → stagnation
        # iter 3 (after replan) → pass
        if review_calls["n"] >= 3:
            return ReviewResult(passed=True, feedback="")
        return ReviewResult(passed=False, feedback="exact same problem here")

    monkeypatch.setattr("cascade.core.call_planner", fake_planner)
    monkeypatch.setattr("cascade.core.call_implementer", fake_implementer)
    monkeypatch.setattr("cascade.core.call_reviewer", fake_reviewer)

    result = await run_cascade(task="x", store=store, s=base_settings)
    assert result.status == "done"
    # Planner called exactly twice — original + 1 replan triggered by stagnation.
    # If stagnation didn't trigger, it would have waited 10 failures.
    assert planner_calls["n"] == 2
    assert result.iterations == 3


async def test_stagnation_with_exhausted_replan_aborts_run(
    monkeypatch, store, base_settings,
):
    """When stagnation persists AND replan budget is gone, the run must end
    with status='failed' — NOT loop until max_iterations. Regression test
    for the infinite-loop hazard introduced by setting max_iterations=999."""
    s = base_settings.model_copy(update={"cascade_replan_max": 1})

    bad = Plan(
        summary="bad",
        steps=[],
        files_to_touch=["x.txt"],
        acceptance_criteria=[],
        quality_checks=[QualityCheck(name="ok", command="true", timeout_s=5)],
    )
    planner_calls = {"n": 0}

    async def fake_planner(*_a, **_kw):
        planner_calls["n"] += 1
        return bad

    impl = ImplementerOutput(ops=[FileOp(op="write", path="x.txt", content="x")])

    async def fake_implementer(*_a, **_kw):
        return impl

    async def fake_reviewer(*_a, **_kw):
        # Same feedback every time → stagnation will fire.
        return ReviewResult(passed=False, feedback="never gonna pass")

    monkeypatch.setattr("cascade.core.call_planner", fake_planner)
    monkeypatch.setattr("cascade.core.call_implementer", fake_implementer)
    monkeypatch.setattr("cascade.core.call_reviewer", fake_reviewer)

    result = await run_cascade(task="x", store=store, s=s)
    assert result.status == "failed"
    assert result.error == "stagnation_replan_exhausted"
    # Without the hard escape this would have been ~999 iterations.
    assert result.iterations <= 6


async def test_no_stagnation_when_feedback_changes(
    monkeypatch, store, base_settings,
):
    """Different feedback every iter → stagnation does NOT fire. Run goes
    through `replan_after_failures` like before."""
    s = base_settings.model_copy(update={
        "cascade_replan_after_failures": 3,
        "cascade_replan_max": 1,
    })

    bad = Plan(
        summary="bad",
        steps=[],
        files_to_touch=["x.txt"],
        acceptance_criteria=[],
        quality_checks=[QualityCheck(name="ok", command="true", timeout_s=5)],
    )
    good = Plan(
        summary="good",
        steps=[],
        files_to_touch=["x.txt"],
        acceptance_criteria=[],
        quality_checks=[QualityCheck(name="ok", command="true", timeout_s=5)],
    )
    plans = iter([bad, good])
    planner_calls = {"n": 0}

    async def fake_planner(*_a, **_kw):
        planner_calls["n"] += 1
        return next(plans)

    impl = ImplementerOutput(ops=[FileOp(op="write", path="x.txt", content="x")])

    async def fake_implementer(*_a, **_kw):
        return impl

    review_calls = {"n": 0}

    async def fake_reviewer(*_a, **_kw):
        review_calls["n"] += 1
        # iter 1, 2, 3 → DIFFERENT feedback every time
        if review_calls["n"] >= 4:
            return ReviewResult(passed=True, feedback="")
        return ReviewResult(passed=False, feedback=f"problem #{review_calls['n']}")

    monkeypatch.setattr("cascade.core.call_planner", fake_planner)
    monkeypatch.setattr("cascade.core.call_implementer", fake_implementer)
    monkeypatch.setattr("cascade.core.call_reviewer", fake_reviewer)

    result = await run_cascade(task="x", store=store, s=s)
    assert result.status == "done"
    # Replan only fired once after 3 different fails → 2 planner calls total.
    assert planner_calls["n"] == 2
    assert result.iterations == 4
