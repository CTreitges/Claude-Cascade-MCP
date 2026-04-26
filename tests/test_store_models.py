from __future__ import annotations

from pathlib import Path

import pytest

from cascade.store import Store


@pytest.fixture
async def store(tmp_path: Path) -> Store:
    s = await Store.open(tmp_path / "test.db")
    yield s
    await s.close()


async def test_set_chat_model_persists_per_worker(store: Store) -> None:
    await store.set_chat_model(42, "implementer", "glm-5.1")
    await store.set_chat_model(42, "planner", "claude-sonnet-4-6")
    sess = await store.get_chat_session(42)
    assert sess["implementer_model"] == "glm-5.1"
    assert sess["planner_model"] == "claude-sonnet-4-6"
    assert sess["reviewer_model"] is None


async def test_set_chat_model_overwrites(store: Store) -> None:
    await store.set_chat_model(1, "implementer", "qwen3-coder:480b")
    await store.set_chat_model(1, "implementer", "kimi-k2.6")
    sess = await store.get_chat_session(1)
    assert sess["implementer_model"] == "kimi-k2.6"


async def test_set_chat_model_clears_with_none(store: Store) -> None:
    await store.set_chat_model(1, "reviewer", "claude-opus-4-7")
    await store.set_chat_model(1, "reviewer", None)
    sess = await store.get_chat_session(1)
    assert sess["reviewer_model"] is None


async def test_set_chat_model_rejects_unknown_worker(store: Store) -> None:
    with pytest.raises(ValueError):
        await store.set_chat_model(1, "executor", "x")


async def test_set_chat_model_accepts_chat_worker(store: Store) -> None:
    await store.set_chat_model(42, "chat", "claude-haiku-4-5")
    sess = await store.get_chat_session(42)
    assert sess["chat_model"] == "claude-haiku-4-5"


async def test_session_includes_model_keys_when_only_repo_set(store: Store) -> None:
    await store.set_chat_repo(7, "/tmp/repo")
    sess = await store.get_chat_session(7)
    assert sess["repo_path"] == "/tmp/repo"
    assert "planner_model" in sess
    assert sess["planner_model"] is None
