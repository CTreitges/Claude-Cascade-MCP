"""Tests for the Triage-Refactor: memory_block routing, path-prevalidation,
and direct_action validation against the simple_actions allowlist.
"""

from __future__ import annotations

from cascade.config import Settings
from cascade.triage import _validate_direct_action, triage


def test_validate_direct_action_accepts_known_kind_under_allowlist(tmp_path):
    da = {
        "kind": "write_file",
        "summary": "drop a file",
        "params": {"target": str(tmp_path / "ok.txt"), "content": "hi"},
    }
    out = _validate_direct_action(da)
    assert out is da


def test_validate_direct_action_rejects_unknown_kind():
    da = {"kind": "rm_rf", "params": {"target": "/tmp/x"}}
    assert _validate_direct_action(da) is None


def test_validate_direct_action_rejects_missing_target():
    da = {"kind": "write_file", "params": {}}
    assert _validate_direct_action(da) is None


def test_validate_direct_action_rejects_target_outside_allowlist():
    da = {
        "kind": "write_file",
        "params": {"target": "/etc/passwd", "content": "x"},
    }
    assert _validate_direct_action(da) is None


def test_validate_direct_action_accepts_tilde_path_in_home_subdir():
    # ~/.config is in the allowlist — both the literal expanded form and
    # the explicit one must validate.
    import os
    da = {
        "kind": "edit_env",
        "params": {
            "target": os.path.expanduser("~/.config/scdl/.env"),
            "key": "FOO", "value": "1",
        },
    }
    assert _validate_direct_action(da) is da


async def test_triage_disabled_short_circuits():
    s = Settings(cascade_triage_enabled=False)
    out = await triage("hello world", s=s)
    assert out.via == "disabled"
    assert out.is_task is True
    assert out.task == "hello world"


async def test_triage_returns_heuristic_on_llm_failure(monkeypatch):
    """When agent_chat raises LLMClientError, triage falls back to the regex
    heuristic — user always gets *some* answer, never an exception."""
    import cascade.triage as triage_mod
    from cascade.llm_client import LLMClientError

    async def boom(**kw):
        raise LLMClientError("simulated provider down")
    monkeypatch.setattr(triage_mod, "agent_chat", boom)

    s = Settings(cascade_triage_enabled=True)
    out = await triage("erstelle hello.py das hi druckt", s=s, lang="de")
    assert out.via == "heuristic"
    assert out.is_task is True


async def test_triage_drops_invalid_direct_action_paths(monkeypatch):
    """LLM proposes a direct_action with a target outside the allowlist —
    triage returns the cascade dispatch instead of the bogus action."""
    import cascade.triage as triage_mod

    async def fake_agent(**kw):
        # Use proper JSON syntax (not Python repr) so parse_json_payload accepts it.
        return (
            '{"is_task": true, "task": "drop file", '
            '"direct_action": {"kind": "write_file", '
            '"summary": "drop", '
            '"params": {"target": "/etc/danger.txt", "content": "x"}}}'
        )
    monkeypatch.setattr(triage_mod, "agent_chat", fake_agent)

    s = Settings(cascade_triage_enabled=True)
    out = await triage("drop file please", s=s, lang="en")
    assert out.is_task is True
    assert out.direct_action is None  # rejected by validator


async def test_triage_keeps_valid_direct_action(monkeypatch, tmp_path):
    import cascade.triage as triage_mod

    target = tmp_path / "ok.txt"

    async def fake_agent(**kw):
        return (
            '{"is_task": true, "task": "drop", '
            '"direct_action": {"kind": "write_file", '
            '"summary": "drop", '
            f'"params": {{"target": "{target}", "content": "x"}}}}}}'
        )
    monkeypatch.setattr(triage_mod, "agent_chat", fake_agent)

    s = Settings(cascade_triage_enabled=True)
    out = await triage("drop file please", s=s, lang="en")
    assert out.direct_action is not None
    assert out.direct_action["kind"] == "write_file"


async def test_triage_threads_memory_block_into_system_prompt(monkeypatch):
    """`memory_block` must reach agent_chat's system_prompt verbatim — that's
    how the bot passes USER FACTS / RECENT UPLOADS / CONVERSATION downstream."""
    import cascade.triage as triage_mod
    captured = {}

    async def fake_agent(**kw):
        captured.update(kw)
        return '{"is_task": false, "reply": "ok"}'
    monkeypatch.setattr(triage_mod, "agent_chat", fake_agent)

    s = Settings(cascade_triage_enabled=True)
    block = "=== RECENT UPLOADS ===\n- foo.json (google_service_account)"
    await triage("hast du die json?", memory_block=block, s=s, lang="de")
    assert "RECENT UPLOADS" in captured["system_prompt"]
    assert "foo.json" in captured["system_prompt"]


async def test_triage_falls_back_to_legacy_context_when_no_memory_block(monkeypatch):
    """When memory_block is None, the older context+history path still works."""
    import cascade.triage as triage_mod
    captured = {}

    async def fake_agent(**kw):
        captured.update(kw)
        return '{"is_task": false, "reply": "ok"}'
    monkeypatch.setattr(triage_mod, "agent_chat", fake_agent)

    s = Settings(cascade_triage_enabled=True)
    await triage(
        "hi",
        context="task1: did stuff",
        history=[{"role": "user", "text": "earlier", "ts": 0}],
        s=s, lang="de",
    )
    assert "Vorheriger Kontext" in captured["system_prompt"]
    assert "Bisheriger Chat-Verlauf" in captured["system_prompt"]
    assert "earlier" in captured["system_prompt"]
