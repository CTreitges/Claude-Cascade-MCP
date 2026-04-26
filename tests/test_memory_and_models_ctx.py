"""Tests for the memory module (JSONL fallback) and num_ctx routing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---- num_ctx ----


def test_implementer_ctx_returns_known_values():
    from cascade.models import implementer_ctx

    for tag in [
        "qwen3-coder:480b",
        "qwen3.5:397b",
        "glm-5.1",
        "kimi-k2.6",
        "minimax-m2.7",
        "deepseek-v4-flash",
    ]:
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


# ---- BM25 recall ----


def test_tokenize_filters_stopwords_and_short_tokens():
    from cascade.memory import _tokenize

    toks = _tokenize("The quick brown FOX jumps for the lazy dog at 12 am")
    assert "the" not in toks
    assert "for" not in toks
    assert "quick" in toks
    assert "brown" in toks
    assert "fox" in toks
    # 2-letter "am" + "12" are below min_len=3 / not alnum-only enough
    assert "am" not in toks


def test_tokenize_handles_german_stopwords_and_diacritics():
    from cascade.memory import _tokenize

    toks = _tokenize("Der Bot soll für mich die Datei ablegen")
    assert "der" not in toks  # stopword
    assert "die" not in toks  # stopword
    assert "für" not in toks  # stopword
    assert "datei" in toks
    assert "ablegen" in toks


async def test_recall_ranks_relevant_above_unrelated(isolated_memory: Path):
    from cascade.memory import recall_context, remember_finding

    # Lots of noise + one clearly relevant finding about JSON credentials
    for i in range(8):
        await remember_finding(f"unrelated note number {i}", tags="misc")
    await remember_finding(
        "user uploaded google service account JSON credentials for soundcloud project",
        tags="credentials,google,scdl",
        importance="high",
    )
    await remember_finding(
        "discussion about telegram bot startup procedure",
        tags="bot,startup",
    )

    out = await recall_context("google credentials json soundcloud")
    assert out is not None
    # The relevant entry must come FIRST
    first_line = out.splitlines()[0]
    assert "google service account" in first_line.lower()


async def test_recall_short_keywords_now_match(isolated_memory: Path):
    """Old impl required >4-char words — now min_len=3, so 'json' (4) and
    'env' (3) should both work."""
    from cascade.memory import recall_context, remember_finding

    await remember_finding("placed json file in config dir", tags="config")
    await remember_finding("set FOO env in .env file", tags="env")

    out_json = await recall_context("the json")  # only 'json' is 4 chars
    assert out_json is not None
    assert "json" in out_json.lower()

    out_env = await recall_context("env file")  # both 3-char
    assert out_env is not None
    assert "env" in out_env.lower()


async def test_recall_importance_boost(isolated_memory: Path):
    """All else equal, a `high`/`critical` importance entry should rank
    above a `low` one with the same content overlap."""
    from cascade.memory import recall_context, remember_finding

    await remember_finding(
        "drive folder id setup procedure",
        tags="drive", importance="low",
    )
    # Add some noise so n_docs > 2
    for i in range(5):
        await remember_finding(f"noise {i}", tags="noise")
    await remember_finding(
        "drive folder id setup procedure",
        tags="drive", importance="critical",
    )

    out = await recall_context("drive folder id setup")
    assert out is not None
    # critical should appear before low
    lines = out.splitlines()
    crit_idx = next(i for i, ln in enumerate(lines) if "critical" in ln)
    low_idx = next(i for i, ln in enumerate(lines) if "/low " in ln)
    assert crit_idx < low_idx


async def test_recall_returns_none_when_no_match(isolated_memory: Path):
    from cascade.memory import recall_context, remember_finding

    await remember_finding("apple banana cherry", tags="fruit")
    out = await recall_context("xenoarchitecture quaternary")
    assert out is None
