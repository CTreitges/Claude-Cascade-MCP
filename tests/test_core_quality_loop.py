"""Verify that quality checks gate the cascade loop (failed check → fail
the iteration even if the reviewer says pass)."""

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
        cascade_max_iterations=3,
        cascade_db_path=tmp_path / "core.db",
        cascade_implementer_provider="ollama",
        cascade_implementer_model="qwen3-coder:480b",
    )


def _patch(monkeypatch, plan, impls, revs):
    impl_iter = iter(impls)
    rev_iter = iter(revs)

    async def fake_planner(_t, **_k):
        return plan

    async def fake_implementer(_p, **_k):
        try:
            return next(impl_iter)
        except StopIteration:
            return ImplementerOutput(ops=[])

    async def fake_reviewer(_p, _d, **_k):
        try:
            return next(rev_iter)
        except StopIteration:
            return ReviewResult(passed=True, feedback="")

    monkeypatch.setattr("cascade.core.call_planner", fake_planner)
    monkeypatch.setattr("cascade.core.call_implementer", fake_implementer)
    monkeypatch.setattr("cascade.core.call_reviewer", fake_reviewer)


async def test_passing_quality_check_lets_run_succeed(monkeypatch, store, s):
    plan = Plan(
        summary="create hello.py",
        steps=["write file"],
        files_to_touch=["hello.py"],
        acceptance_criteria=["prints hi"],
        quality_checks=[QualityCheck(
            name="exists", command="test -f hello.py", timeout_s=5
        )],
    )
    impl = ImplementerOutput(ops=[FileOp(op="write", path="hello.py", content="print('hi')\n")])
    review = ReviewResult(passed=True, feedback="")
    _patch(monkeypatch, plan, [impl], [review])
    result = await run_cascade(task="x", store=store, s=s)
    assert result.status == "done"
    assert result.iterations == 1


async def test_failing_quality_check_overrides_reviewer_pass(monkeypatch, store, s):
    plan = Plan(
        summary="x",
        steps=[],
        files_to_touch=["never.py"],
        acceptance_criteria=[],
        quality_checks=[QualityCheck(
            name="must-exist", command="test -f never.py", timeout_s=5
        )],
    )
    # Implementer doesn't actually write the file
    impl = ImplementerOutput(ops=[])
    # Reviewer would happily say pass=True without the gate
    review = ReviewResult(passed=True, feedback="looks fine")
    _patch(monkeypatch, plan, [impl, impl, impl], [review, review, review])
    result = await run_cascade(task="x", store=store, s=s)
    assert result.status == "failed", "failed check must override reviewer pass"
    iters = await store.list_iterations(result.task_id)
    # Every iteration that ran should have reviewer_pass=False after the gate.
    runtime_iters = [i for i in iters if i.n > 0]
    assert all(i.reviewer_pass is False for i in runtime_iters)


async def test_eventually_passing_check_completes(monkeypatch, store, s):
    """First implementer creates wrong file, second creates correct one — should
    converge in 2 iterations."""
    plan = Plan(
        summary="x",
        steps=[],
        files_to_touch=["target.txt"],
        acceptance_criteria=[],
        quality_checks=[QualityCheck(
            name="exists", command="test -f target.txt", timeout_s=5
        )],
    )
    bad = ImplementerOutput(ops=[FileOp(op="write", path="other.txt", content="x")])
    good = ImplementerOutput(ops=[FileOp(op="write", path="target.txt", content="ok")])
    review = ReviewResult(passed=True, feedback="")
    _patch(monkeypatch, plan, [bad, good], [review, review])
    result = await run_cascade(task="x", store=store, s=s)
    assert result.status == "done"
    assert result.iterations == 2


async def test_no_checks_means_reviewer_decides(monkeypatch, store, s):
    plan = Plan(
        summary="x",
        # steps must be non-empty to clear core.py:_plan_is_actionable.
        # The point of THIS test is the absence of quality_checks, not
        # the absence of all plan content.
        steps=["do the thing"],
        files_to_touch=[],
        acceptance_criteria=[],
        quality_checks=[],  # explicit empty
    )
    impl = ImplementerOutput(ops=[])
    review = ReviewResult(passed=True, feedback="")
    _patch(monkeypatch, plan, [impl], [review])
    result = await run_cascade(task="x", store=store, s=s)
    assert result.status == "done"
