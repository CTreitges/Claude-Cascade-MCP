"""Tests for skill storage, suggester gating, and template rendering."""

from __future__ import annotations

from pathlib import Path

import pytest

from cascade.config import Settings
from cascade.store import Store


@pytest.fixture
async def store(tmp_path: Path) -> Store:
    s = await Store.open(tmp_path / "skills.db")
    yield s
    await s.close()


# ---- store ----


async def test_create_and_list_skill(store: Store) -> None:
    sid = await store.create_skill(
        name="pytest_for_file",
        description="Generate pytest tests for a file",
        task_template="Generate pytest tests for {file}",
        rationale="recurring",
        source_task_ids=["a", "b"],
    )
    assert sid > 0
    skills = await store.list_skills()
    assert len(skills) == 1
    assert skills[0]["name"] == "pytest_for_file"
    assert skills[0]["usage_count"] == 0


async def test_get_skill_by_name(store: Store) -> None:
    await store.create_skill(name="x", description="y", task_template="z {arg}")
    sk = await store.get_skill_by_name("x")
    assert sk is not None
    assert sk["task_template"] == "z {arg}"
    assert await store.get_skill_by_name("nope") is None


async def test_increment_skill_usage(store: Store) -> None:
    await store.create_skill(name="x", description=None, task_template="x")
    await store.increment_skill_usage("x")
    await store.increment_skill_usage("x")
    sk = await store.get_skill_by_name("x")
    assert sk["usage_count"] == 2
    assert sk["last_used_at"] is not None


async def test_delete_skill(store: Store) -> None:
    await store.create_skill(name="x", description=None, task_template="x")
    assert await store.delete_skill("x") is True
    assert await store.delete_skill("x") is False
    assert await store.get_skill_by_name("x") is None


async def test_unique_skill_name_constraint(store: Store) -> None:
    await store.create_skill(name="dup", description=None, task_template="x")
    with pytest.raises(Exception):
        await store.create_skill(name="dup", description=None, task_template="y")


async def test_skill_suggestion_round_trip(store: Store) -> None:
    # Need a real task to satisfy the FK
    tid = await store.create_task(source="cli", task_text="t")
    sug = {"should_create": True, "name": "x", "task_template": "x"}
    await store.record_skill_suggestion(tid, sug, chat_id=42)
    got = await store.get_skill_suggestion(tid)
    assert got["chat_id"] == 42
    assert got["suggestion"]["name"] == "x"
    await store.mark_skill_suggestion_decided(tid, "accepted")
    got2 = await store.get_skill_suggestion(tid)
    assert got2["decision"] == "accepted"


# ---- suggester gating ----


async def test_suggester_skips_when_too_few_recent_tasks(monkeypatch) -> None:
    from cascade import skill_suggester as mod

    called = {"n": 0}

    async def fake_claude_call(**_kw):
        called["n"] += 1
        from cascade.claude_cli import ClaudeResult
        return ClaudeResult(text='{"should_create": true, "name": "x", "task_template": "x"}', raw=None, duration_s=0.1)

    monkeypatch.setattr(mod, "claude_call", fake_claude_call)

    class T:
        def __init__(self, tid, text):
            self.id = tid
            self.task_text = text
            self.status = "done"
            self.created_at = 0

    sug = await mod.maybe_suggest_skill(
        current_task=T("c", "x"),
        recent_tasks=[T("a", "x")],  # only 1 → below threshold of 2
        s=Settings(),
    )
    assert sug is None
    assert called["n"] == 0


async def test_suggester_respects_cooldown(monkeypatch) -> None:
    import time as time_mod
    from cascade import skill_suggester as mod

    called = {"n": 0}

    async def fake_claude_call(**_kw):
        called["n"] += 1
        from cascade.claude_cli import ClaudeResult
        return ClaudeResult(text='{"should_create": true, "name": "x", "task_template": "x"}', raw=None, duration_s=0.1)

    monkeypatch.setattr(mod, "claude_call", fake_claude_call)

    class T:
        def __init__(self, tid, text):
            self.id = tid
            self.task_text = text
            self.status = "done"
            self.created_at = 0

    sug = await mod.maybe_suggest_skill(
        current_task=T("c", "x"),
        recent_tasks=[T("a", "x"), T("b", "x")],
        s=Settings(),
        cooldown_s=300,
        last_suggested_at=time_mod.time(),  # just now → on cooldown
    )
    assert sug is None
    assert called["n"] == 0


async def test_suggester_returns_none_when_should_create_false(monkeypatch) -> None:
    from cascade import skill_suggester as mod

    async def fake(**_kw):
        from cascade.claude_cli import ClaudeResult
        return ClaudeResult(
            text='{"should_create": false, "rationale": "one-off"}', raw=None, duration_s=0.1
        )

    monkeypatch.setattr(mod, "claude_call", fake)

    class T:
        def __init__(self, tid, text):
            self.id = tid
            self.task_text = text
            self.status = "done"
            self.created_at = 0

    sug = await mod.maybe_suggest_skill(
        current_task=T("c", "x"),
        recent_tasks=[T("a", "x"), T("b", "x")],
        s=Settings(),
        last_suggested_at=None,
    )
    assert sug is None


async def test_suggester_returns_suggestion_on_pattern(monkeypatch) -> None:
    from cascade import skill_suggester as mod

    async def fake(**_kw):
        from cascade.claude_cli import ClaudeResult
        return ClaudeResult(
            text='{"should_create": true, "name": "pytest_for_file", '
                 '"description": "Generate pytest tests", '
                 '"task_template": "Generate pytest tests for {file}", '
                 '"placeholders": ["file"], "rationale": "two pytest tasks recently"}',
            raw=None, duration_s=0.1,
        )

    monkeypatch.setattr(mod, "claude_call", fake)

    class T:
        def __init__(self, tid, text):
            self.id = tid
            self.task_text = text
            self.status = "done"
            self.created_at = 0

    sug = await mod.maybe_suggest_skill(
        current_task=T("c", "Erstelle pytest für foo.py"),
        recent_tasks=[T("a", "Erstelle pytest für bar.py"), T("b", "Erstelle pytest für baz.py")],
        s=Settings(),
        last_suggested_at=None,
    )
    assert sug is not None
    assert sug.name == "pytest_for_file"
    assert sug.placeholders == ["file"]
    assert "{file}" in sug.task_template


# ---- template rendering (matches cmd_run_skill behavior) ----


def test_template_positional_substitution() -> None:
    template = "Erstelle pytest für {0}"
    out = template.format(*["foo.py"])
    assert out == "Erstelle pytest für foo.py"


def test_template_named_substitution() -> None:
    template = "Erstelle pytest für {file} mit Fokus auf {aspect}"
    out = template.format(**{"file": "foo.py", "aspect": "edge cases"})
    assert "foo.py" in out and "edge cases" in out
