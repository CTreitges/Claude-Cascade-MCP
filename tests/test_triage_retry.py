"""Tests for the with_retry / triage timeout-handling integration.

The bug observed in production (Drive-Setup log 2026-04-26 12:55):

    triage llm call failed (claude agent call failed:
    claude -p timed out after 60s) — falling back to heuristic

Root cause: claude_cli.py raised `ClaudeCliError` on `asyncio.TimeoutError`,
which propagated as `LLMClientError` to triage.py — bypassing
`with_retry` entirely and dumping the user onto the Heuristic-fallback.

These tests pin the new behaviour:
  - timeouts are now `RateLimitError`
  - `with_retry` therefore picks them up and retries
  - tight retry budgets propagate from agent_chat → with_retry
"""

from __future__ import annotations

import asyncio

import pytest

from cascade.rate_limit import (
    RateLimitError,
    is_rate_limit,
    with_retry,
)


def test_is_rate_limit_recognises_timeout_text():
    assert is_rate_limit("claude -p timed out after 60s")
    assert is_rate_limit("connection reset by peer")
    assert is_rate_limit("HTTP 429 too many requests")
    assert not is_rate_limit("invalid argument: foo")


async def test_with_retry_recovers_from_rate_limit():
    """One transient failure → with_retry sleeps + retries, second call wins."""
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RateLimitError("transient timeout", retry_after=0.01)
        return "ok"

    out = await with_retry(
        factory, min_backoff_s=0.0, max_backoff_s=0.1, label="test",
    )
    assert out == "ok"
    assert calls["n"] == 2


async def test_with_retry_gives_up_after_total_wait_exceeded():
    """Once total_waited > max_total_wait_s, the last RateLimitError re-raises."""

    async def always_fails():
        raise RateLimitError("permanent overload", retry_after=0.05)

    with pytest.raises(RateLimitError):
        await with_retry(
            always_fails,
            min_backoff_s=0.0,
            max_backoff_s=0.05,
            max_total_wait_s=0.1,  # tiny budget → fails fast
            label="test",
        )


async def test_with_retry_cancellation_via_event():
    cancel = asyncio.Event()

    async def hangs():
        raise RateLimitError("flap", retry_after=10.0)

    async def trigger():
        await asyncio.sleep(0.05)
        cancel.set()

    task = asyncio.create_task(trigger())
    with pytest.raises(asyncio.CancelledError):
        await with_retry(
            hangs,
            min_backoff_s=0.0,
            max_backoff_s=10.0,
            cancel_event=cancel,
            label="test",
        )
    await task


async def test_claude_cli_timeout_now_raises_rate_limit_error(monkeypatch):
    """The reverted-bug regression test: TimeoutError in claude_cli must
    surface as RateLimitError, not ClaudeCliError. Otherwise triage would
    flip to heuristic before with_retry got a chance."""
    from cascade.claude_cli import claude_call

    # Patch subprocess + timeout to simulate a hung CLI.
    class _FakeProc:
        returncode = 0

        async def communicate(self, input=None):
            await asyncio.sleep(10)  # never finishes within wait_for
            return b"", b""

        def kill(self):
            pass

        async def wait(self):
            return 0

    async def _spawn(*args, **kw):
        return _FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _spawn,
    )
    with pytest.raises(RateLimitError) as ei:
        await claude_call(
            prompt="x", model="claude-sonnet-4-6",
            system_prompt="s", timeout_s=0.05,
        )
    assert "timed out" in str(ei.value).lower()


def test_parse_retry_after_recognises_days():
    """User asked: cascade soll automatisch auf nächste Session warten.
    Claude's weekly cap surfaces as 'Resets in N days' — must parse."""
    from cascade.rate_limit import parse_retry_after
    assert parse_retry_after("Resets in 3 days") == 3 * 86400
    assert parse_retry_after("Resets in 1 day") == 1 * 86400
    assert parse_retry_after("try again in 7 days") == 7 * 86400
    assert parse_retry_after("available in 2 days") == 2 * 86400
    # Pre-existing patterns still work alongside.
    assert parse_retry_after("Resets in 2 hours") == 2 * 3600
    assert parse_retry_after("retry-after: 60") == 60


def test_with_retry_default_budget_is_one_week():
    """7 days = enough to survive Claude's weekly-usage cap automatically."""
    import inspect
    from cascade.rate_limit import with_retry
    sig = inspect.signature(with_retry)
    assert sig.parameters["max_total_wait_s"].default == 7 * 86400


async def test_agent_chat_threads_retry_kwargs(monkeypatch):
    """agent_chat must pass retry-budget kwargs through to with_retry —
    otherwise the tight triage budget gets swallowed and we wait 12h."""
    import cascade.llm_client as llm_mod

    captured = {}

    async def fake_with_retry(factory, *, label, **kwargs):
        captured.update(kwargs)
        captured["label"] = label
        # Pretend the LLM said "hi" — bypass the actual call.
        return "hi"

    monkeypatch.setattr(llm_mod, "with_retry", fake_with_retry, raising=False)
    # Patch via the module's `from .rate_limit import with_retry` import.
    import cascade.rate_limit as rl
    monkeypatch.setattr(rl, "with_retry", fake_with_retry, raising=False)

    # The local import happens inside agent_chat; we need to patch sys.modules
    # of the `with_retry` symbol in scope. Simpler: stub claude_call and
    # observe through it.
    async def fake_claude(*, prompt, model, system_prompt, **kw):
        return type("R", (), {"text": "hi"})()
    import cascade.claude_cli as cc
    monkeypatch.setattr(cc, "claude_call", fake_claude)

    from cascade.llm_client import agent_chat
    out = await agent_chat(
        prompt="hi",
        model="claude-sonnet-4-6",
        system_prompt="s",
        retry_max_total_wait_s=180.0,
        retry_min_backoff_s=10.0,
        retry_max_backoff_s=60.0,
    )
    assert out == "hi"
    assert captured.get("max_total_wait_s") == 180.0
    assert captured.get("min_backoff_s") == 10.0
    assert captured.get("max_backoff_s") == 60.0
