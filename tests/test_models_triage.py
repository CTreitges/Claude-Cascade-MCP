from __future__ import annotations


from cascade.config import Settings
from cascade.models import (
    IMPLEMENTER_MODELS,
    PLANNER_REVIEWER_MODELS,
    implementer_display,
    implementer_provider,
)
from cascade.triage import _heuristic, triage


def test_implementer_catalog_contents():
    tags = set(IMPLEMENTER_MODELS.keys())
    assert tags == {
        "qwen3-coder:480b",
        "qwen3.5:397b",
        "glm-5.1",
        "kimi-k2.6",
        "minimax-m2.7",
        "deepseek-v4-flash",
        "claude-sonnet-4-6",
        "claude-opus-4-7",
    }


def test_planner_reviewer_has_opus_and_sonnet():
    tags = set(PLANNER_REVIEWER_MODELS.keys())
    assert "claude-opus-4-7" in tags
    assert "claude-sonnet-4-6" in tags


def test_chat_models_contains_three_models():
    from cascade.models import CHAT_MODELS
    assert set(CHAT_MODELS) >= {"claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-7"}


def test_effort_levels_per_model():
    from cascade.models import effort_levels_for, model_supports_effort
    # Opus + Sonnet: full set incl. extended-thinking
    assert "xhigh" in effort_levels_for("claude-opus-4-7")
    assert "max" in effort_levels_for("claude-sonnet-4-6")
    # Haiku: light only
    assert effort_levels_for("claude-haiku-4-5") == ("low", "medium", "high")
    # Ollama models: empty (no effort knob exists in Ollama API)
    assert effort_levels_for("qwen3-coder:480b") == ()
    assert effort_levels_for("glm-5.1") == ()
    assert effort_levels_for(None) == ()
    # capability flag
    assert model_supports_effort("claude-sonnet-4-6") is True
    assert model_supports_effort("qwen3-coder:480b") is False
    assert model_supports_effort(None) is False


def test_implementer_provider_routing():
    assert implementer_provider("qwen3-coder:480b") == "ollama"
    assert implementer_provider("glm-5.1") == "ollama"
    assert implementer_provider("claude-sonnet-4-6") == "claude"
    assert implementer_provider("claude-opus-4-7") == "claude"
    # unknown tags fall back to ollama (cloud catalog is the historic default)
    assert implementer_provider("unknown-model") == "ollama"


def test_implementer_display_falls_back_to_tag():
    assert implementer_display("glm-5.1") == "GLM 5.1"
    assert implementer_display("deepseek-v4-flash") == "DeepSeek V4 Flash"
    assert implementer_display("qwen3-coder:480b") == "Qwen3 Coder 480B"
    assert implementer_display("claude-sonnet-4-6") == "Claude Sonnet 4.6"
    assert implementer_display("claude-opus-4-7") == "Claude Opus 4.7"
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

    async def fake_agent_chat(**_kw):
        from cascade.llm_client import LLMClientError
        raise LLMClientError("simulated failure")

    monkeypatch.setattr(triage_mod, "agent_chat", fake_agent_chat)

    s = Settings(cascade_triage_enabled=True)
    r = await triage_mod.triage("Hey", lang="de", s=s)
    assert r.via == "heuristic"


async def test_triage_uses_claude_when_available(monkeypatch):
    from cascade import triage as triage_mod

    async def fake_agent_chat(**_kw):
        return '{"is_task": true, "task": "build foo"}'

    monkeypatch.setattr(triage_mod, "agent_chat", fake_agent_chat)

    s = Settings(cascade_triage_enabled=True)
    r = await triage_mod.triage("please build foo", lang="en", s=s)
    assert r.via == "claude"
    assert r.is_task is True
    assert r.task == "build foo"


async def test_triage_history_is_included_in_system_prompt(monkeypatch):
    from cascade import triage as triage_mod

    captured: dict = {}

    async def fake_agent_chat(**kw):
        captured.update(kw)
        return '{"is_task": false, "reply": "ok"}'

    monkeypatch.setattr(triage_mod, "agent_chat", fake_agent_chat)

    s = Settings(cascade_triage_enabled=True)
    history = [
        {"role": "user", "text": "wie lief der letzte task?", "ts": 0.0},
        {"role": "bot", "text": "1 Iteration, alles grün.", "ts": 1.0},
    ]
    await triage_mod.triage("und der davor?", lang="de", s=s, history=history)
    sys_prompt = captured.get("system_prompt") or ""
    assert "Bisheriger Chat-Verlauf" in sys_prompt
    assert "wie lief der letzte task" in sys_prompt
    assert "1 Iteration" in sys_prompt


async def test_triage_returns_reply_for_chat(monkeypatch):
    from cascade import triage as triage_mod

    async def fake_agent_chat(**_kw):
        return '{"is_task": false, "reply": "Hi there!"}'

    monkeypatch.setattr(triage_mod, "agent_chat", fake_agent_chat)

    s = Settings(cascade_triage_enabled=True)
    r = await triage_mod.triage("hi", lang="en", s=s)
    assert r.via == "claude"
    assert r.is_task is False
    assert r.reply == "Hi there!"
