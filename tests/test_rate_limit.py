from __future__ import annotations

import asyncio

import pytest

from cascade.rate_limit import (
    RateLimitError,
    is_rate_limit,
    parse_retry_after,
    with_retry,
)


def test_is_rate_limit_detects_common_signals():
    assert is_rate_limit("HTTP 429 Too Many Requests")
    assert is_rate_limit("rate limit exceeded")
    assert is_rate_limit("Claude usage limit reached. Resets in 2 hours.")
    assert is_rate_limit('{"type":"overloaded_error","message":"..."}')
    assert is_rate_limit("Weekly limit reached, please try later")
    assert is_rate_limit("session limit hit")
    assert is_rate_limit("Resource exhausted")


def test_is_rate_limit_passes_normal_errors():
    assert not is_rate_limit("File not found")
    assert not is_rate_limit("ValueError: invalid path")
    assert not is_rate_limit("")
    assert not is_rate_limit(None)


def test_parse_retry_after_handles_known_phrasings():
    assert parse_retry_after("Resets in 2 hours") == 7200
    assert parse_retry_after("Try again in 30 minutes") == 1800
    assert parse_retry_after("retry-after: 60") == 60
    assert parse_retry_after("available in 4 hours") == 14400
    assert parse_retry_after("nothing here") is None
    assert parse_retry_after(None) is None


async def test_with_retry_returns_immediately_on_success():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return "ok"

    out = await with_retry(factory, min_backoff_s=0.01, max_backoff_s=0.05)
    assert out == "ok"
    assert calls["n"] == 1


async def test_with_retry_retries_after_rate_limit_then_succeeds():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RateLimitError("rate-limit", retry_after=0.01)
        return "ok"

    out = await with_retry(factory, min_backoff_s=0.01, max_backoff_s=0.05)
    assert out == "ok"
    assert calls["n"] == 3


async def test_with_retry_gives_up_after_total_wait_cap():
    async def factory():
        raise RateLimitError("nope", retry_after=10.0)

    with pytest.raises(RateLimitError):
        await with_retry(factory, min_backoff_s=0.01, max_total_wait_s=0.02)


async def test_with_retry_invokes_on_wait_callback():
    seen: list[tuple[float, int]] = []

    async def on_wait(secs, attempt, _msg):
        seen.append((secs, attempt))

    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] < 2:
            raise RateLimitError("rl", retry_after=0.01)
        return "ok"

    await with_retry(factory, min_backoff_s=0.01, on_wait=on_wait)
    assert len(seen) == 1
    assert seen[0][1] == 1


async def test_with_retry_aborts_on_cancel():
    cancel = asyncio.Event()

    async def factory():
        raise RateLimitError("rl", retry_after=10.0)

    async def trigger_cancel():
        await asyncio.sleep(0.05)
        cancel.set()

    asyncio.create_task(trigger_cancel())
    with pytest.raises(asyncio.CancelledError):
        await with_retry(
            factory, min_backoff_s=0.01, cancel_event=cancel,
        )
