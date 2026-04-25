from __future__ import annotations

import pytest

from cascade.config import Settings
from cascade.models import (
    IMPLEMENTER_MODELS,
    PLANNER_REVIEWER_MODELS,
    implementer_display,
    implementer_provider,
)
from cascade.triage import _heuristic, triage


def test_implementer_catalog_contains_user_requested_models():
    tags = set(IMPLEMENTER_MODELS.keys())
    assert "qwen3-coder:480b" in tags
    assert "glm-5.1" in tags
    assert "minimax-m2.7" in tags
    assert "deepseek-v4-flash" in tags
    assert "kimi-k2.6" in tags


def test_planner_reviewer_has_opus_and_sonnet():
    tags = set(PLANNER_REVIEWER_MODELS.keys())
    assert "claude-opus-4-7" in tags
    assert "claude-sonnet-4-6" in tags


def test_implementer_provider_default_is_ollama():
    assert implementer_provider("qwen3-coder:480b") == "ollama"
    assert implementer_provider("glm-5.1") == "ollama"
    assert implementer_provider("unknown-model") == "ollama"


def test_implementer_display_falls_back_to_tag():
    assert implementer_display("glm-5.1") == "GLM 5.1"
    assert implementer_display("custom-tag-xyz") == "custom-tag-xyz"


# ---- triage ----


def test_heuristic_imperative_de_is_task():
    r = _heuristic("Erstelle hello.py das hi druckt", "de")
    assert r.is_task is True
    assert r.via == "heuristic"


def test_heuristic_smalltalk_is_chat():
    r = _heuristic("Hey, was geht?", "de")
    assert r.is_task is False
    assert r.reply


def test_heuristic_imperative_en():
    r = _heuristic("create a small script", "en")
    assert r.is_task is True


async def test_triage_disabled_returns_task_passthrough(monkeypatch):
    s = Settings(cascade_triage_enabled=False)
    r = await triage("just a hello", lang="de", s=s)
    assert r.is_task is True
    assert r.task == "just a hello"
    assert r.via == "disabled"


async def test_triage_falls_back_when_claude_fails(monkeypatch):
    from cascade import triage as triage_mod

    async def fake_claude_call(**_kw):
        from cascade.claude_cli import ClaudeCliError
        raise ClaudeCliError("simulated failure")

    monkeypatch.setattr(triage_mod, "claude_call", fake_claude_call)

    s = Settings(cascade_triage_enabled=True)
    r = await triage_mod.triage("Hey", lang="de", s=s)
    assert r.via == "heuristic"


async def test_triage_uses_haiku_when_available(monkeypatch):
    from cascade import triage as triage_mod
    from cascade.claude_cli import ClaudeResult

    async def fake_claude_call(**_kw):
        return ClaudeResult(text='{"is_task": true, "task": "build foo"}', raw=None, duration_s=0.1)

    monkeypatch.setattr(triage_mod, "claude_call", fake_claude_call)

    s = Settings(cascade_triage_enabled=True)
    r = await triage_mod.triage("please build foo", lang="en", s=s)
    assert r.via == "haiku"
    assert r.is_task is True
    assert r.task == "build foo"


async def test_triage_returns_reply_for_chat(monkeypatch):
    from cascade import triage as triage_mod
    from cascade.claude_cli import ClaudeResult

    async def fake_claude_call(**_kw):
        return ClaudeResult(text='{"is_task": false, "reply": "Hi there!"}', raw=None, duration_s=0.1)

    monkeypatch.setattr(triage_mod, "claude_call", fake_claude_call)

    s = Settings(cascade_triage_enabled=True)
    r = await triage_mod.triage("hi", lang="en", s=s)
    assert r.via == "haiku"
    assert r.is_task is False
    assert r.reply == "Hi there!"
