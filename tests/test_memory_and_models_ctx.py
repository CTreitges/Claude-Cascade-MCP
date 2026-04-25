"""Tests for the memory module (JSONL fallback) and num_ctx routing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---- num_ctx ----


def test_implementer_ctx_returns_known_values():
    from cascade.models import implementer_ctx

    for tag in ["qwen3-coder:480b", "glm-5.1", "kimi-k2.6", "minimax-m2.7", "deepseek-v4-flash"]:
        assert implementer_ctx(tag) >= 100_000


def test_implementer_ctx_falls_back_for_unknown():
    from cascade.models import implementer_ctx, DEFAULT_IMPLEMENTER_CTX

    assert implementer_ctx("totally-new-tag-xyz") == DEFAULT_IMPLEMENTER_CTX


async def test_ollama_call_passes_num_ctx_in_options(monkeypatch):
    """Verify the ollama AsyncClient.chat() receives num_ctx in options."""
    import sys
    import cascade.llm_client as mod
    from cascade.config import Settings

    captured = {}

    class FakeClient:
        def __init__(self, **_kw):
            pass

        async def chat(self, *, model, messages, format, options):
            captured["options"] = options
            captured["model"] = model
            return {"message": {"content": '{"ok": true}'}}

    fake_module = type("FakeOllama", (), {"AsyncClient": FakeClient})
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    s = Settings(
        cascade_implementer_provider="ollama",
        cascade_implementer_model="kimi-k2.6",
    )
    await mod.implementer_chat(system="x", user="y", model="kimi-k2.6", s=s)
    assert "num_ctx" in captured["options"]
    assert captured["options"]["num_ctx"] >= 100_000


# ---- memory JSONL ----


@pytest.fixture
def isolated_memory(tmp_path: Path, monkeypatch):
    """Redirect cascade_home so memory.jsonl lands in tmp_path."""
    from cascade.config import Settings
    fake = Settings(cascade_home=tmp_path)
    import cascade.memory as mod
    monkeypatch.setattr(mod, "settings", lambda: fake)
    return tmp_path


async def test_remember_writes_jsonl(isolated_memory: Path):
    from cascade.memory import remember_finding

    ok = await remember_finding(
        "test entry one",
        category="finding",
        importance="high",
        tags="claude-cascade,test",
    )
    assert ok is True
    path = isolated_memory / "store" / "memory.jsonl"
    assert path.exists()
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["content"] == "test entry one"
    assert entry["category"] == "finding"
    assert entry["importance"] == "high"
    assert entry["project"] == "claude-cascade"


async def test_remember_decision_routes_correctly(isolated_memory: Path):
    from cascade.memory import remember_decision

    await remember_decision("important call", importance="high")
    path = isolated_memory / "store" / "memory.jsonl"
    entry = json.loads(path.read_text().splitlines()[0])
    assert entry["category"] == "decision"


async def test_recall_finds_keyword_matches(isolated_memory: Path):
    from cascade.memory import recall_context, remember_finding

    await remember_finding("Implementing pytest fixtures for foo module", tags="pytest")
    await remember_finding("Refactor click CLI", tags="cli")
    await remember_finding("Random other unrelated note", tags="misc")

    out = await recall_context("pytest fixtures for the new module")
    assert out is not None
    assert "pytest" in out.lower() or "fixtures" in out.lower()


async def test_recall_returns_none_when_empty(isolated_memory: Path):
    from cascade.memory import recall_context

    out = await recall_context("anything")
    assert out is None


async def test_remember_handles_extra_dict(isolated_memory: Path):
    from cascade.memory import remember_finding

    await remember_finding("with extra", extra={"task_id": "abc123", "iters": 3})
    entry = json.loads((isolated_memory / "store" / "memory.jsonl").read_text().splitlines()[0])
    assert entry["task_id"] == "abc123"
    assert entry["iters"] == 3
