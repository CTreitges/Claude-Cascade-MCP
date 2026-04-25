"""Verify the stdin-fallback for claude_call when prompts are large enough
to trip Linux ARG_MAX (~128 kB). Regression test for the v0.8 reviewer-call
crash 'OSError: [Errno 7] Argument list too long'."""

from __future__ import annotations

import asyncio


class _FakeProc:
    returncode = 0

    async def communicate(self, input=None):
        self._captured_stdin = input
        return (b'{"result":"{\\"ok\\":true}"}', b"")


async def test_short_prompt_passed_via_argv(monkeypatch):
    import cascade.claude_cli as mod

    captured = {}

    async def fake_create(*args, **kw):
        captured["args"] = args
        captured["stdin_kw"] = kw.get("stdin")
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    await mod.claude_call(
        prompt="short", model="claude-sonnet-4-6", output_json=True
    )
    # short prompt → argv carries it, no PIPE
    assert "short" in captured["args"]
    assert captured["stdin_kw"] is None


async def test_large_prompt_sent_via_stdin(monkeypatch):
    import cascade.claude_cli as mod

    captured = {}

    async def fake_create(*args, **kw):
        captured["args"] = args
        captured["stdin_kw"] = kw.get("stdin")
        proc = _FakeProc()
        captured["proc"] = proc
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    big_prompt = "x" * 50_000  # well over the 8 kB threshold
    await mod.claude_call(
        prompt=big_prompt, model="claude-sonnet-4-6", output_json=True
    )
    # argv must NOT carry the giant prompt
    assert big_prompt not in captured["args"]
    # subprocess opened with stdin=PIPE
    assert captured["stdin_kw"] is asyncio.subprocess.PIPE
    # stdin actually received the prompt
    assert captured["proc"]._captured_stdin == big_prompt.encode("utf-8")


async def test_large_system_prompt_also_triggers_stdin(monkeypatch):
    import cascade.claude_cli as mod

    captured = {}

    async def fake_create(*args, **kw):
        captured["args"] = args
        captured["stdin_kw"] = kw.get("stdin")
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    await mod.claude_call(
        prompt="hi",
        system_prompt="y" * 20_000,
        model="claude-sonnet-4-6",
        output_json=True,
    )
    assert captured["stdin_kw"] is asyncio.subprocess.PIPE
    # short user-prompt is not in argv either when stdin path is used
    assert "hi" not in captured["args"]
