"""End-to-end-ish triage tests: walk all three paths (chat / direct_action /
cascade) with the LLM mocked, verifying that the right code branches fire.

Doesn't test the bot's Telegram handlers — those need an Application — just
the triage→core boundary that the handlers ride on top of.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cascade.config import Settings
from cascade.simple_actions import is_known_kind, run_action
from cascade.store import Store
from cascade.triage import triage


@pytest.fixture
async def store(tmp_path: Path) -> Store:
    s = await Store.open(tmp_path / "e2e.db")
    yield s
    await s.close()


# ---------- Mode 3: Conversation ----------


async def test_triage_chat_mode_returns_friendly_reply(monkeypatch):
    import cascade.triage as triage_mod

    async def fake_agent(**kw):
        return '{"is_task": false, "reply": "Klar, was möchtest du wissen?"}'
    monkeypatch.setattr(triage_mod, "agent_chat", fake_agent)

    s = Settings(cascade_triage_enabled=True)
    out = await triage("hi, wie gehts?", s=s, lang="de")
    assert out.is_task is False
    assert "wissen" in (out.reply or "")
    assert out.direct_action is None


# ---------- Mode 1: Direct Action ----------


async def test_triage_to_simple_action_round_trip(monkeypatch, tmp_path):
    """Triage → produces direct_action → simple_actions executes it."""
    import cascade.triage as triage_mod

    target = tmp_path / "out.txt"

    async def fake_agent(**kw):
        return (
            '{"is_task": true, "task": "drop file", '
            '"direct_action": {"kind": "write_file", '
            '"summary": "drop file", '
            f'"params": {{"target": "{target}", "content": "hello\\n"}}}}}}'
        )
    monkeypatch.setattr(triage_mod, "agent_chat", fake_agent)

    s = Settings(cascade_triage_enabled=True)
    out = await triage("Schreib hello in out.txt", s=s, lang="de")
    assert out.is_task is True
    assert out.direct_action is not None
    assert out.direct_action["kind"] == "write_file"
    assert is_known_kind(out.direct_action["kind"])

    res = await run_action(out.direct_action)
    assert res.ok, res.error
    assert target.exists()
    assert target.read_text() == "hello\n"


async def test_triage_unsafe_target_routes_to_cascade(monkeypatch):
    """LLM picks a known kind but a path outside the allowlist → triage
    DROPS the direct_action so the supervisor falls back to a full cascade,
    rather than handing the bot a crash-on-execute."""
    import cascade.triage as triage_mod

    async def fake_agent(**kw):
        return (
            '{"is_task": true, "task": "drop file", '
            '"direct_action": {"kind": "write_file", '
            '"summary": "drop", '
            '"params": {"target": "/etc/secrets.txt", "content": "x"}}}'
        )
    monkeypatch.setattr(triage_mod, "agent_chat", fake_agent)

    s = Settings(cascade_triage_enabled=True)
    out = await triage("schreib was nach /etc/secrets.txt", s=s, lang="de")
    assert out.is_task is True
    assert out.direct_action is None  # validator rejected


# ---------- Mode 2: Full Cascade ----------


async def test_triage_full_task_no_direct_action(monkeypatch):
    import cascade.triage as triage_mod

    async def fake_agent(**kw):
        return (
            '{"is_task": true, "task": "Refactor pipeline.py to use polars instead of pandas"}'
        )
    monkeypatch.setattr(triage_mod, "agent_chat", fake_agent)

    s = Settings(cascade_triage_enabled=True)
    out = await triage(
        "kannst du pipeline.py auf polars umstellen?",
        s=s, lang="de",
    )
    assert out.is_task is True
    assert out.direct_action is None
    assert "polars" in out.task


# ---------- Memory-block routing ----------


async def test_triage_with_memory_block_in_system_prompt(monkeypatch):
    import cascade.triage as triage_mod
    captured = {}

    async def fake_agent(**kw):
        captured.update(kw)
        return '{"is_task": false, "reply": "ok"}'
    monkeypatch.setattr(triage_mod, "agent_chat", fake_agent)

    block = (
        "=== USER FACTS ===\n"
        "- credential.google_service_account.path: /home/u/.config/sa.json\n"
        "=== RECENT UPLOADS ===\n"
        "- foo.json (google_service_account)\n"
    )
    s = Settings(cascade_triage_enabled=True)
    await triage("hast du die json?", memory_block=block, s=s, lang="de")
    assert "credential.google_service_account.path" in captured["system_prompt"]
    assert "foo.json" in captured["system_prompt"]


# ---------- Heuristic fallback ----------


async def test_triage_heuristic_when_llm_crashes(monkeypatch):
    import cascade.triage as triage_mod
    from cascade.llm_client import LLMClientError

    async def boom(**kw):
        raise LLMClientError("simulated outage")
    monkeypatch.setattr(triage_mod, "agent_chat", boom)

    s = Settings(cascade_triage_enabled=True)
    out = await triage(
        "erstelle einen kleinen helper", s=s, lang="de",
    )
    assert out.via == "heuristic"
    assert out.is_task is True


async def test_triage_disabled_setting_passes_through_as_task():
    s = Settings(cascade_triage_enabled=False)
    out = await triage("just talking", s=s, lang="de")
    assert out.via == "disabled"
    assert out.is_task is True
    assert out.task == "just talking"
