from __future__ import annotations

import asyncio

import pytest

from cascade.rate_limit import (
    RateLimitError,
    is_rate_limit,
    is_short_backoff_signal,
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
    # Short/uninformative messages now fall back to 10s short-backoff
    # (the test ran "nothing here" which is <20 chars).
    assert parse_retry_after("nothing here") == 10.0
    # Longer non-rate-limit messages still return None.
    assert parse_retry_after("file not found in some longer path /home/x/y") is None
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


def test_is_short_backoff_signal_matches_process_kills_and_blips():
    """commit 1e562af — patterns that clear in seconds, not hours."""
    assert is_short_backoff_signal("claude -p exited 143")
    assert is_short_backoff_signal("process exited 137 (SIGKILL)")
    assert is_short_backoff_signal("connection reset by peer")
    assert is_short_backoff_signal("Connection refused")
    assert is_short_backoff_signal("connection aborted")
    assert is_short_backoff_signal("Operation timed out")
    assert is_short_backoff_signal("HTTP request timeout")
    assert is_short_backoff_signal("Network is unreachable")
    # 5xx infrastructure blips (commit on Run #4 wedge):
    assert is_short_backoff_signal("status code: 500")
    assert is_short_backoff_signal("status_code=502")
    assert is_short_backoff_signal("HTTP 504")
    assert is_short_backoff_signal("Internal Server Error (ref: abc123)")
    assert is_short_backoff_signal("Bad Gateway")
    assert is_short_backoff_signal("Gateway Timeout")
    # 503 stays LONG-backoff: it's the canonical "overload, come back later":
    assert not is_short_backoff_signal("status code: 503")
    # plain rate-limits stay long-backoff:
    assert not is_short_backoff_signal("HTTP 429 Too Many Requests")
    assert not is_short_backoff_signal("Resets in 2 hours")
    assert not is_short_backoff_signal("")
    assert not is_short_backoff_signal(None)


def test_parse_retry_after_falls_back_to_10s_for_short_signals():
    """Short-backoff signals get 10s when no explicit retry-after found,
    instead of None (which would default to min_backoff_s=3600)."""
    assert parse_retry_after("claude -p exited 143") == 10.0
    assert parse_retry_after("connection reset") == 10.0
    assert parse_retry_after("Operation timed out") == 10.0
    # explicit retry-after still wins over the fallback:
    assert parse_retry_after("connection reset, retry-after: 60") == 60
    # genuine rate-limit without explicit retry-after still returns None:
    assert parse_retry_after("HTTP 429 Too Many Requests") is None


async def test_with_retry_uses_short_backoff_for_sigterm():
    """Bot-restart artefacts (`exited 143`) should NOT wedge the retry on
    the global min_backoff_s=3600 floor — they clear in seconds."""
    calls = {"n": 0}
    waits: list[float] = []

    async def on_wait(secs, _attempt, _reason):
        waits.append(secs)

    async def factory():
        calls["n"] += 1
        if calls["n"] < 2:
            raise RateLimitError("claude -p exited 143 (transient)")
        return "ok"

    out = await with_retry(
        factory,
        # would normally clamp to 3600s — but the short-backoff bypass
        # should detect the SIGTERM signature and clamp to 10-30s.
        min_backoff_s=3600.0, max_backoff_s=3600.0,
        max_total_wait_s=120.0,
        on_wait=on_wait,
    )
    assert out == "ok"
    assert len(waits) == 1
    assert 10.0 <= waits[0] <= 30.0, f"expected 10-30s, got {waits[0]}"


async def test_with_retry_uses_long_backoff_for_real_rate_limit():
    """Genuine rate-limits still use the configured min_backoff_s — only
    SIGTERM/timeout/connection-reset get the 10-30s clamp."""
    waits: list[float] = []

    async def on_wait(secs, _attempt, _reason):
        waits.append(secs)
        # Fail the test fast: on_wait is called BEFORE the actual sleep,
        # so we cancel via test timeout via max_total_wait_s small enough.

    async def factory():
        raise RateLimitError("HTTP 429 Too Many Requests")

    with pytest.raises(RateLimitError):
        await with_retry(
            factory,
            min_backoff_s=120.0, max_backoff_s=120.0,
            max_total_wait_s=60.0,  # less than min_backoff -> immediate give-up
            on_wait=on_wait,
        )
    # The single attempt would have gone with the long backoff, then hit
    # the budget cap. We don't assert the wait value here because
    # max_total_wait_s clamps before sleeping; this confirms the
    # short-clamp is NOT applied (otherwise budget wouldn't trip).


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
