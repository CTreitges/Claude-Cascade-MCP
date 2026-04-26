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


async def test_chat_messages_round_trip(store: Store) -> None:
    await store.append_chat_message(7, "user", "hallo")
    await store.append_chat_message(7, "bot", "hi")
    await store.append_chat_message(7, "user", "was hast du gemacht?")
    msgs = await store.recent_chat_messages(7, limit=10)
    assert [m["role"] for m in msgs] == ["user", "bot", "user"]
    assert msgs[0]["text"] == "hallo"
    assert msgs[-1]["text"] == "was hast du gemacht?"


async def test_chat_messages_isolated_per_chat(store: Store) -> None:
    await store.append_chat_message(1, "user", "a")
    await store.append_chat_message(2, "user", "b")
    one = await store.recent_chat_messages(1)
    two = await store.recent_chat_messages(2)
    assert [m["text"] for m in one] == ["a"]
    assert [m["text"] for m in two] == ["b"]


async def test_chat_messages_prune_to_max_keep(store: Store) -> None:
    for i in range(8):
        await store.append_chat_message(5, "user", f"m{i}", max_keep=3)
    msgs = await store.recent_chat_messages(5, limit=10)
    assert [m["text"] for m in msgs] == ["m5", "m6", "m7"]


async def test_chat_messages_clear(store: Store) -> None:
    await store.append_chat_message(11, "user", "a")
    await store.append_chat_message(11, "bot", "b")
    n = await store.clear_chat_messages(11)
    assert n == 2
    assert await store.recent_chat_messages(11) == []


async def test_chat_messages_empty_text_ignored(store: Store) -> None:
    await store.append_chat_message(3, "user", "")
    await store.append_chat_message(3, "user", "   ")
    assert await store.recent_chat_messages(3) == []


async def test_chat_messages_invalid_role_raises(store: Store) -> None:
    with pytest.raises(ValueError):
        await store.append_chat_message(3, "system", "x")


async def test_chat_question_round_trip(store: Store) -> None:
    qid = await store.create_chat_question(7, "approve plan?", task_id="abc")
    pending = await store.get_pending_question(7)
    assert pending and pending["id"] == qid
    assert pending["question"] == "approve plan?"
    assert pending["answered_at"] is None
    await store.answer_chat_question(qid, "yes go ahead")
    pending2 = await store.get_pending_question(7)
    assert pending2 is None  # no longer pending
    row = await store.get_question(qid)
    assert row and row["answer"] == "yes go ahead"
    assert row["answered_at"] is not None


async def test_chat_question_expire(store: Store) -> None:
    qid = await store.create_chat_question(7, "?")
    await store.expire_chat_question(qid)
    assert await store.get_pending_question(7) is None
    row = await store.get_question(qid)
    assert row and row["expired_at"] is not None and row["answered_at"] is None
