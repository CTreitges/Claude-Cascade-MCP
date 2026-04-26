from __future__ import annotations

from pathlib import Path

import pytest

from cascade.chat_memory import ChatMemory, ChatMemoryConfig
from cascade.store import Store


@pytest.fixture
async def store(tmp_path: Path) -> Store:
    s = await Store.open(tmp_path / "memtest.db")
    yield s
    await s.close()


@pytest.fixture
def memory(store: Store) -> ChatMemory:
    return ChatMemory(store, config=ChatMemoryConfig(
        limit_hot=5, limit_warm=3, limit_fts=4,
        file_content_chars=200, pending_attachments_hours=24,
    ))


async def test_append_persists_message_and_returns_id(
    memory: ChatMemory, store: Store,
) -> None:
    mid = await memory.append(42, "user", "hallo welt")
    assert mid > 0
    rows = await store.recent_chat_messages(42, limit=10)
    assert len(rows) == 1
    assert rows[0]["text"] == "hallo welt"
    assert rows[0]["role"] == "user"
    assert rows[0]["file_path"] is None


async def test_append_with_file_inlines_content_and_registers_attachment(
    memory: ChatMemory, store: Store,
) -> None:
    cls = {"kind": "google_service_account", "summary": "SA for project X"}
    await memory.append(
        7, "user", "hier die json",
        file_path="/tmp/sa.json",
        file_content='{"type":"service_account","project_id":"foo"}',
        file_classification=cls,
    )
    msgs = await store.recent_chat_messages(7, limit=10)
    assert len(msgs) == 1
    assert msgs[0]["file_path"] == "/tmp/sa.json"
    assert "service_account" in msgs[0]["file_content"]
    assert msgs[0]["file_classification"]["kind"] == "google_service_account"

    # Pending attachments mirror
    atts = await store.list_pending_attachments(7)
    assert len(atts) == 1
    assert atts[0]["file_name"] == "sa.json"
    assert atts[0]["classification"]["kind"] == "google_service_account"
    assert atts[0]["handled"] is False


async def test_append_caps_file_content_at_30kb(
    memory: ChatMemory, store: Store,
) -> None:
    big = "x" * 50_000
    await memory.append(1, "user", "huge", file_path="/tmp/x.txt", file_content=big)
    msgs = await store.recent_chat_messages(1, limit=1)
    assert len(msgs[0]["file_content"]) == 30_000


async def test_build_context_includes_user_facts(
    memory: ChatMemory, store: Store,
) -> None:
    await store.set_user_fact(99, "credential.google_sa.path", "/home/u/sa.json")
    await memory.append(99, "user", "yo")
    ctx = await memory.build_context(99, lang="de")
    assert "NUTZER-FAKTEN" in ctx
    assert "credential.google_sa.path" in ctx
    assert "/home/u/sa.json" in ctx


async def test_build_context_includes_recent_uploads_and_chat(
    memory: ChatMemory, store: Store,
) -> None:
    cls = {"kind": "google_service_account", "summary": "SA SCDL"}
    await memory.append(
        5, "user", "ich schicke dir eine json",
        file_path="/home/u/.config/scdl/sa.json",
        file_content='{"type":"service_account"}',
        file_classification=cls,
    )
    await memory.append(5, "bot", "datei abgelegt")
    await memory.append(5, "user", "alles klar")

    ctx = await memory.build_context(5, lang="de")
    assert "KüRZLICH HOCHGELADENE DATEIEN" in ctx
    assert "sa.json" in ctx
    assert "google_service_account" in ctx
    assert "CHAT-VERLAUF" in ctx
    assert "ich schicke dir eine json" in ctx
    assert "datei abgelegt" in ctx
    # File content is inlined in the conversation block
    assert "service_account" in ctx


async def test_build_context_emits_search_hits_for_old_messages(
    memory: ChatMemory, store: Store,
) -> None:
    # Push more than limit_hot messages so older "marker" message scrolls out
    cfg = memory.config
    await memory.append(11, "user", "the marker word: airlock-banana")
    for i in range(cfg.limit_hot + 2):
        await memory.append(11, "user", f"filler {i}")
    # Marker should have scrolled out of Hot tier
    ctx = await memory.build_context(11, query="airlock-banana", lang="de")
    # Either via SEARCH HITS or, if FTS5 fallback retrieves it, also fine —
    # what matters is that the marker is reachable when queried.
    assert "airlock-banana" in ctx


async def test_clear_chat_messages_wipes_summaries_and_attachments(
    memory: ChatMemory, store: Store,
) -> None:
    await memory.append(
        3, "user", "hi", file_path="/tmp/a.json",
        file_content='{"x":1}', file_classification={"kind": "generic_json"},
    )
    await store.add_chat_summary(
        3, period_from=0.0, period_to=1.0,
        summary="old talk", msg_count=10,
    )
    assert (await store.list_pending_attachments(3))
    assert (await store.recent_chat_summaries(3))

    n = await store.clear_chat_messages(3)
    assert n >= 1
    assert (await store.list_pending_attachments(3)) == []
    assert (await store.recent_chat_summaries(3)) == []
    assert (await store.recent_chat_messages(3)) == []


async def test_search_chat_messages_finds_file_content(
    memory: ChatMemory, store: Store,
) -> None:
    await memory.append(
        2, "user", "config attached",
        file_path="/tmp/conf.json",
        file_content='{"DRIVE_FOLDER_ID":"abc-secret-marker"}',
        file_classification={"kind": "generic_config_json"},
    )
    hits = await store.search_chat_messages(2, "abc-secret-marker", limit=5)
    assert any("abc-secret-marker" in (h.get("file_content") or "") for h in hits)


async def test_pending_attachment_marked_handled(memory: ChatMemory, store: Store) -> None:
    await memory.append(
        4, "user", "datei",
        file_path="/tmp/x.json", file_content='{"a":1}',
        file_classification={"kind": "generic_json"},
    )
    atts = await store.list_pending_attachments(4)
    assert len(atts) == 1
    await store.mark_attachment_handled(atts[0]["id"], task_id="abc123")
    atts2 = await store.list_pending_attachments(4)
    assert atts2[0]["handled"] is True
    assert atts2[0]["handled_by_task_id"] == "abc123"


async def test_candidates_for_summary_respects_age(
    memory: ChatMemory, store: Store,
) -> None:
    import time as _t
    now = _t.time()
    # Insert two old-stamp rows directly (bypass append's now-stamping)
    async with store._tx() as c:
        await c.execute(
            """INSERT INTO chat_messages
                 (chat_id, role, text, ts, summarized)
               VALUES (?, ?, ?, ?, 0), (?, ?, ?, ?, 0)""",
            (
                10, "user", "old1", now - 30 * 86400,
                10, "bot",  "old2", now - 30 * 86400,
            ),
        )
    # And one fresh message
    await memory.append(10, "user", "fresh")
    cands = await memory.candidates_for_summary(10)
    texts = [c["text"] for c in cands]
    assert "old1" in texts
    assert "old2" in texts
    assert "fresh" not in texts
