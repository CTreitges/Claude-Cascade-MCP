from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cascade.feedback import ask_user
from cascade.store import Store


@pytest.fixture
async def store(tmp_path: Path) -> Store:
    s = await Store.open(tmp_path / "test.db")
    yield s
    await s.close()


async def test_ask_user_returns_when_answered(store: Store) -> None:
    """Simulates the user replying mid-poll: ask_user should pick it up
    and return the answer string."""
    chat_id = 42

    async def answer_after_delay() -> None:
        await asyncio.sleep(0.05)
        pending = await store.get_pending_question(chat_id)
        assert pending is not None
        await store.answer_chat_question(pending["id"], "ja, mach")

    asyncio.create_task(answer_after_delay())
    out = await ask_user(store, chat_id, "weiter?", timeout_s=2.0)
    assert out == "ja, mach"


async def test_ask_user_times_out_to_fallback(store: Store) -> None:
    out = await ask_user(
        store, 42, "?", timeout_s=0.05, fallback="default-no",
    )
    assert out == "default-no"
    pending = await store.get_pending_question(42)
    assert pending is None  # expired, not pending


async def test_ask_user_aborts_on_cancel(store: Store) -> None:
    cancel = asyncio.Event()

    async def trigger_cancel() -> None:
        await asyncio.sleep(0.05)
        cancel.set()

    asyncio.create_task(trigger_cancel())
    out = await ask_user(
        store, 42, "?", timeout_s=2.0, fallback="aborted", cancel_event=cancel,
    )
    assert out == "aborted"


async def test_ask_user_with_no_chat_id_returns_fallback(store: Store) -> None:
    out = await ask_user(store, 0, "?", fallback="no-chat")
    assert out == "no-chat"
