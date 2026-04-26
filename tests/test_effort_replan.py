"""Tests for /effort and /replan persistence + claude_cli effort flag."""

from __future__ import annotations

from pathlib import Path

import pytest

from cascade.config import Settings
from cascade.store import Store


@pytest.fixture
async def store(tmp_path: Path) -> Store:
    s = await Store.open(tmp_path / "test.db")
    yield s
    await s.close()


# ---- store ----


async def test_set_chat_effort_persists(store: Store) -> None:
    await store.set_chat_effort(1, "planner", "high")
    await store.set_chat_effort(1, "reviewer", "low")
    sess = await store.get_chat_session(1)
    assert sess["planner_effort"] == "high"
    assert sess["reviewer_effort"] == "low"
    assert sess["triage_effort"] is None


async def test_set_chat_effort_clears(store: Store) -> None:
    await store.set_chat_effort(1, "triage", "high")
    await store.set_chat_effort(1, "triage", None)
    sess = await store.get_chat_session(1)
    assert sess["triage_effort"] is None


async def test_set_chat_effort_rejects_unknown_worker(store: Store) -> None:
    with pytest.raises(ValueError):
        await store.set_chat_effort(1, "bogus", "high")


async def test_set_chat_effort_implementer(store: Store) -> None:
    await store.set_chat_effort(1, "implementer", "high")
    sess = await store.get_chat_session(1)
    assert sess and sess["implementer_effort"] == "high"


async def test_set_chat_temperature_persists(store: Store) -> None:
    await store.set_chat_temperature(1, "implementer", 0.7)
    await store.set_chat_temperature(1, "chat", 0.0)
    sess = await store.get_chat_session(1)
    assert sess
    assert sess["implementer_temperature"] == 0.7
    assert sess["chat_temperature"] == 0.0


async def test_set_chat_temperature_clear(store: Store) -> None:
    await store.set_chat_temperature(1, "planner", 0.5)
    await store.set_chat_temperature(1, "planner", None)
    sess = await store.get_chat_session(1)
    assert sess and sess["planner_temperature"] is None


async def test_set_chat_temperature_rejects_unknown(store: Store) -> None:
    with pytest.raises(ValueError):
        await store.set_chat_temperature(1, "bogus", 0.5)


async def test_set_chat_replan_max(store: Store) -> None:
    await store.set_chat_replan_max(1, 4)
    sess = await store.get_chat_session(1)
    assert sess["replan_max"] == 4
    await store.set_chat_replan_max(1, None)
    sess2 = await store.get_chat_session(1)
    assert sess2["replan_max"] is None


# ---- claude_cli effort flag ----


async def test_claude_call_includes_effort_flag(monkeypatch):
    """Snapshot the args list passed to create_subprocess_exec to confirm
    --effort lands at the right spot."""
    import asyncio
    import cascade.claude_cli as mod

    captured = {}

    class FakeProc:
        returncode = 0
        async def communicate(self, input=None):
            return (b'{"result":"{\\"ok\\":true}","total_cost_usd":0.001}', b"")

    async def fake_create(*args, **_kw):
        captured["args"] = args
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    await mod.claude_call(
        prompt="hi", model="claude-sonnet-4-6", output_json=True, effort="high"
    )
    assert "--effort" in captured["args"]
    idx = captured["args"].index("--effort")
    assert captured["args"][idx + 1] == "high"
    # Must come after --model
    assert captured["args"].index("--model") < idx


async def test_claude_call_omits_effort_when_none(monkeypatch):
    import asyncio
    import cascade.claude_cli as mod

    captured = {}

    class FakeProc:
        returncode = 0
        async def communicate(self, input=None):
            captured["stdin"] = input
            return (b'{"result":""}', b"")

    async def fake_create(*args, **_kw):
        captured["args"] = args
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    await mod.claude_call(prompt="hi", model="claude-sonnet-4-6", output_json=True)
    assert "--effort" not in captured["args"]


# ---- Settings overrides reach claude_call ----


async def test_planner_call_uses_settings_effort(monkeypatch):
    """When Settings.cascade_planner_effort is set, call_planner forwards it."""
    from cascade.agents import planner as pmod

    captured = {}

    async def fake_agent_chat(**kw):
        captured.update(kw)
        return '{"summary":"x","steps":[],"files_to_touch":[],"acceptance_criteria":[]}'

    monkeypatch.setattr(pmod, "agent_chat", fake_agent_chat)

    s = Settings(cascade_planner_effort="xhigh")
    p = await pmod.call_planner("do thing", s=s)
    assert captured["effort"] == "xhigh"
    assert p.summary == "x"


async def test_reviewer_call_uses_settings_effort(monkeypatch):
    from cascade.agents import reviewer as rmod
    from cascade.agents.planner import Plan

    captured = {}

    async def fake_agent_chat(**kw):
        captured.update(kw)
        return '{"pass": true, "feedback": ""}'

    monkeypatch.setattr(rmod, "agent_chat", fake_agent_chat)

    s = Settings(cascade_reviewer_effort="low")
    plan = Plan(summary="x", steps=[], files_to_touch=[], acceptance_criteria=[])
    await rmod.call_reviewer(plan, "diff", s=s)
    assert captured["effort"] == "low"
