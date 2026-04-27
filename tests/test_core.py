"""End-to-end orchestrator test using monkeypatched agents."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cascade.agents.implementer import ImplementerOutput
from cascade.agents.planner import Plan
from cascade.agents.reviewer import ReviewResult
from cascade.config import Settings
from cascade.core import run_cascade
from cascade.store import Store
from cascade.workspace import FileOp


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


def _patch_planner(monkeypatch, plan: Plan):
    async def fake(_task, **_kw):
        return plan

    monkeypatch.setattr("cascade.core.call_planner", fake)


def _patch_implementer(monkeypatch, outputs: list[ImplementerOutput]):
    iterator = iter(outputs)

    async def fake(_plan, **_kw):
        try:
            return next(iterator)
        except StopIteration:
            return ImplementerOutput(ops=[])

    monkeypatch.setattr("cascade.core.call_implementer", fake)


def _patch_reviewer(monkeypatch, results: list[ReviewResult]):
    iterator = iter(results)

    async def fake(_plan, _diff, **_kw):
        try:
            return next(iterator)
        except StopIteration:
            return ReviewResult(passed=True, feedback="")

    monkeypatch.setattr("cascade.core.call_reviewer", fake)


async def test_pass_first_iteration(monkeypatch, store, s):
    plan = Plan(
        summary="hello",
        steps=["create hello.py"],
        files_to_touch=["hello.py"],
        acceptance_criteria=["prints hi"],
    )
    impl = ImplementerOutput(
        ops=[FileOp(op="write", path="hello.py", content="print('hi')\n")]
    )
    review = ReviewResult(passed=True, feedback="", severity="low")
    _patch_planner(monkeypatch, plan)
    _patch_implementer(monkeypatch, [impl])
    _patch_reviewer(monkeypatch, [review])

    result = await run_cascade(task="make hello.py", store=store, s=s)

    assert result.status == "done"
    assert result.iterations == 1
    assert (result.workspace_path / "hello.py").read_text() == "print('hi')\n"

    t = await store.get_task(result.task_id)
    assert t.status == "done"
    assert t.completed_at is not None
    iters = await store.list_iterations(result.task_id)
    # iter 0 = plan, iter 1 = impl/review
    assert {i.n for i in iters} == {0, 1}


async def test_recovers_after_failed_iteration(monkeypatch, store, s):
    plan = Plan(
        summary="x",
        steps=["s"],
        files_to_touch=["x.py"],
        acceptance_criteria=["c"],
    )
    impl1 = ImplementerOutput(ops=[FileOp(op="write", path="x.py", content="bad")])
    impl2 = ImplementerOutput(
        ops=[FileOp(op="edit", path="x.py", find="bad", replace="good")]
    )
    review_fail = ReviewResult(passed=False, feedback="fix it", severity="medium")
    review_ok = ReviewResult(passed=True, feedback="", severity="low")

    _patch_planner(monkeypatch, plan)
    _patch_implementer(monkeypatch, [impl1, impl2])
    _patch_reviewer(monkeypatch, [review_fail, review_ok])

    result = await run_cascade(task="x", store=store, s=s)
    assert result.status == "done"
    assert result.iterations == 2
    assert (result.workspace_path / "x.py").read_text() == "good"

    iters = await store.list_iterations(result.task_id)
    assert any(i.reviewer_pass is False for i in iters)
    assert any(i.reviewer_pass is True for i in iters)


async def test_fails_after_max_iterations(monkeypatch, store, s):
    plan = Plan(summary="x", steps=[], files_to_touch=[], acceptance_criteria=["c"])
    bad_impl = ImplementerOutput(ops=[FileOp(op="write", path="x.py", content="bad")])
    review_fail = ReviewResult(passed=False, feedback="still bad", severity="medium")

    _patch_planner(monkeypatch, plan)
    _patch_implementer(monkeypatch, [bad_impl] * 5)
    _patch_reviewer(monkeypatch, [review_fail] * 5)

    result = await run_cascade(task="x", store=store, s=s)
    assert result.status == "failed"
    assert result.iterations == 3  # max
    t = await store.get_task(result.task_id)
    assert t.status == "failed"


async def test_planner_failure_marks_task_failed(monkeypatch, store, s):
    async def boom(*_a, **_kw):
        raise RuntimeError("planner blew up")

    monkeypatch.setattr("cascade.core.call_planner", boom)

    result = await run_cascade(task="x", store=store, s=s)
    assert result.status == "failed"
    assert "planner blew up" in (result.error or "")


async def test_progress_callback_receives_events(monkeypatch, store, s):
    # Note: plan must satisfy core.py:_plan_is_actionable (any of subtasks,
    # direct_ops, steps, files_to_touch, acceptance_criteria non-empty).
    plan = Plan(
        summary="s", steps=["s1"], files_to_touch=[], acceptance_criteria=[],
    )
    _patch_planner(monkeypatch, plan)
    _patch_implementer(monkeypatch, [ImplementerOutput(ops=[])])
    _patch_reviewer(monkeypatch, [ReviewResult(passed=True, feedback="")])

    events: list[tuple[str, dict]] = []

    async def cb(_task_id, event, payload):
        events.append((event, payload))

    await run_cascade(task="x", store=store, s=s, progress=cb)

    names = [e for e, _ in events]
    assert "started" in names
    assert "planning" in names
    assert "planned" in names
    assert "implementing" in names
    assert "implemented" in names
    assert "reviewing" in names
    assert "reviewed" in names
    assert "done" in names


async def test_cancel_event_aborts_run(monkeypatch, store, s):
    plan = Plan(summary="s", steps=["s1"], files_to_touch=[], acceptance_criteria=[])
    cancel = asyncio.Event()

    async def slow_planner(*_a, **_kw):
        cancel.set()  # cancel BEFORE returning, so the next checkpoint trips
        return plan

    monkeypatch.setattr("cascade.core.call_planner", slow_planner)

    with pytest.raises(asyncio.CancelledError):
        await run_cascade(task="x", store=store, s=s, cancel_event=cancel)

    t = await store.latest_task()
    assert t.status == "cancelled"


async def test_resume_continues_from_existing_task(monkeypatch, store, s):
    plan = Plan(summary="s", steps=["s1"], files_to_touch=[], acceptance_criteria=[])
    _patch_planner(monkeypatch, plan)
    _patch_implementer(monkeypatch, [ImplementerOutput(ops=[])])
    _patch_reviewer(monkeypatch, [ReviewResult(passed=True, feedback="")])

    # Pre-create an interrupted task
    tid = await store.create_task(source="telegram", task_text="resume me")
    await store.update_task(tid, status="interrupted", iteration=2)

    result = await run_cascade(
        task="resume me", store=store, s=s, resume_task_id=tid
    )
    assert result.task_id == tid
    assert result.status == "done"
    # Should have used iteration ≥ 2 as starting point
    t = await store.get_task(tid)
    assert t.iteration >= 2
