from __future__ import annotations

import pytest

from cascade.config import Settings
from cascade.llm_client import (
    LLMClientError,
    LLMReply,
    implementer_chat,
    schema_as_hint,
)


def test_schema_as_hint_pretty_prints():
    out = schema_as_hint({"type": "object", "properties": {"a": {"type": "integer"}}})
    assert "\n" in out
    assert "\"properties\"" in out


def test_openai_compat_credentials_routing():
    s = Settings(
        glm_api_key="g", deepseek_api_key="d", minimax_api_key="m", kimi_api_key="k"
    )
    assert s.openai_compat_credentials("glm-4.5")[1] == "g"
    assert s.openai_compat_credentials("deepseek-v4")[1] == "d"
    assert s.openai_compat_credentials("minimax-m1")[1] == "m"
    assert s.openai_compat_credentials("kimi-k2")[1] == "k"


def test_openai_compat_unknown_model_raises():
    s = Settings()
    with pytest.raises(ValueError):
        s.openai_compat_credentials("some-random-model-xyz")


async def test_unknown_provider_raises():
    with pytest.raises(LLMClientError):
        await implementer_chat(
            system="s", user="u", provider="unknown", model="x"
        )


async def test_openai_compat_missing_key_raises(monkeypatch):
    s = Settings(cascade_implementer_provider="openai_compatible")
    # glm_api_key is empty → call should fail before any network IO
    with pytest.raises(LLMClientError, match="No API key configured"):
        await implementer_chat(
            system="s", user="u", provider="openai_compatible", model="glm-4.5", s=s
        )


async def test_ollama_path_routes_to_ollama_module(monkeypatch):
    """Smoke: ensure the ollama branch picks up our env and calls AsyncClient.chat."""
    import cascade.llm_client as mod

    captured: dict = {}

    class FakeClient:
        def __init__(self, *, host, headers, timeout):
            captured["host"] = host
            captured["headers"] = headers

        async def chat(self, *, model, messages, format, options):
            captured["model"] = model
            captured["format"] = format
            return {
                "message": {"content": '{"ok": true}'},
                "prompt_eval_count": 5,
                "eval_count": 3,
            }

    fake_module = type("FakeOllama", (), {"AsyncClient": FakeClient})
    monkeypatch.setitem(__import__("sys").modules, "ollama", fake_module)

    s = Settings(
        cascade_implementer_provider="ollama",
        ollama_cloud_api_key="secret",
        ollama_cloud_host="https://ollama.example",
    )
    reply: LLMReply = await mod.implementer_chat(
        system="sys", user="usr", model="qwen3-coder:480b", s=s
    )
    assert reply.text == '{"ok": true}'
    assert reply.provider == "ollama"
    assert reply.model == "qwen3-coder:480b"
    assert captured["headers"] == {"Authorization": "Bearer secret"}
    assert captured["host"] == "https://ollama.example"
    assert captured["format"] == "json"
    assert reply.usage["prompt_eval_count"] == 5


async def test_ollama_empty_content_raises(monkeypatch):
    import cascade.llm_client as mod

    class FakeClient:
        def __init__(self, **_):
            pass

        async def chat(self, **_):
            return {"message": {"content": ""}}

    monkeypatch.setitem(
        __import__("sys").modules, "ollama", type("M", (), {"AsyncClient": FakeClient})
    )
    s = Settings(cascade_implementer_provider="ollama")
    with pytest.raises(LLMClientError, match="empty"):
        await mod.implementer_chat(system="s", user="u", s=s)
