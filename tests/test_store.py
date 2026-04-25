from __future__ import annotations

from pathlib import Path

import pytest

from cascade.store import Store


@pytest.fixture
async def store(tmp_path: Path) -> Store:
    s = await Store.open(tmp_path / "test.db")
    yield s
    await s.close()


async def test_create_and_get_task(store: Store) -> None:
    tid = await store.create_task(
        source="cli",
        task_text="hello",
        implementer_model="qwen3-coder:480b",
        implementer_tools="fileops",
    )
    t = await store.get_task(tid)
    assert t is not None
    assert t.task_text == "hello"
    assert t.status == "pending"
    assert t.implementer_model == "qwen3-coder:480b"
    assert t.iteration == 0
    assert t.metadata == {}


async def test_update_task_status_and_completion(store: Store) -> None:
    tid = await store.create_task(source="cli", task_text="x")
    await store.update_task(tid, status="running", iteration=1)
    await store.update_task(
        tid, status="done", result_summary="all green", completed=True
    )
    t = await store.get_task(tid)
    assert t.status == "done"
    assert t.iteration == 1
    assert t.result_summary == "all green"
    assert t.completed_at is not None


async def test_list_tasks_and_filter_by_status(store: Store) -> None:
    a = await store.create_task(source="cli", task_text="a")
    b = await store.create_task(source="cli", task_text="b")
    await store.update_task(b, status="running")
    all_tasks = await store.list_tasks(limit=10)
    assert {t.id for t in all_tasks} == {a, b}
    running = await store.list_tasks(status="running")
    assert [t.id for t in running] == [b]


async def test_latest_task(store: Store) -> None:
    a = await store.create_task(source="cli", task_text="a")
    import asyncio

    await asyncio.sleep(0.01)
    b = await store.create_task(source="cli", task_text="b")
    latest = await store.latest_task()
    assert latest is not None
    assert latest.id == b
    assert a != b


async def test_mark_running_as_interrupted(store: Store) -> None:
    a = await store.create_task(source="telegram", task_text="a")
    b = await store.create_task(source="telegram", task_text="b")
    await store.update_task(a, status="running")
    await store.update_task(b, status="done", completed=True)
    interrupted = await store.mark_running_as_interrupted()
    assert interrupted == [a]
    ta = await store.get_task(a)
    tb = await store.get_task(b)
    assert ta.status == "interrupted"
    assert tb.status == "done"


async def test_iterations_upsert(store: Store) -> None:
    tid = await store.create_task(source="cli", task_text="t")
    await store.record_iteration(
        tid, 1, implementer_output="ops", reviewer_pass=False, reviewer_feedback="fix"
    )
    await store.record_iteration(
        tid, 1, implementer_output="ops2", reviewer_pass=True, reviewer_feedback=None
    )
    iters = await store.list_iterations(tid)
    assert len(iters) == 1
    assert iters[0].implementer_output == "ops2"
    assert iters[0].reviewer_pass is True


async def test_iterations_ordered_by_n(store: Store) -> None:
    tid = await store.create_task(source="cli", task_text="t")
    await store.record_iteration(tid, 2, reviewer_pass=True)
    await store.record_iteration(tid, 1, reviewer_pass=False)
    iters = await store.list_iterations(tid)
    assert [i.n for i in iters] == [1, 2]


async def test_logs_tail_returns_chronological(store: Store) -> None:
    tid = await store.create_task(source="cli", task_text="t")
    for i in range(5):
        await store.log(tid, "info", f"msg {i}")
    tail = await store.tail_logs(tid, n=3)
    assert [e.message for e in tail] == ["msg 2", "msg 3", "msg 4"]


async def test_chat_session_persists_repo_and_last_task(store: Store) -> None:
    await store.set_chat_repo(42, "/some/repo")
    await store.set_chat_last_task(42, "abc123")
    sess = await store.get_chat_session(42)
    assert sess is not None
    assert sess["repo_path"] == "/some/repo"
    assert sess["last_task_id"] == "abc123"


async def test_chat_session_missing_returns_none(store: Store) -> None:
    sess = await store.get_chat_session(99)
    assert sess is None
