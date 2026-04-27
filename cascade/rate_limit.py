"""Rate-limit / quota-exhaustion auto-retry helper.

Both Claude (Max-Subscription session limit, weekly usage cap, 429 from API
gateway) and Ollama Cloud (429 / 529 overloaded) have transient failure modes
where the right answer is "wait, then try again" — not "fail the run".

`with_retry()` wraps any async callable. It catches `RateLimitError`, sleeps
the duration suggested by the error (or exponential backoff if none), and
retries — up to a hard total-wait cap (default 12h) so we never hang forever.

Detection helpers:
  - `is_rate_limit(text)`  → bool: does this look like a rate-limit/quota error
  - `parse_retry_after(text)` → seconds | None
  - both also accept exception-like objects

The cascade workspace stays untouched while waiting; once the LLM responds
again the iteration just continues.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import re
from typing import Awaitable, Callable

log = logging.getLogger("cascade.rate_limit")


# Cascade-runs that want to surface "we're waiting for the next session
# window" updates set this contextvar to a callback. `with_retry` calls
# it whenever it sleeps. The callback signature is
# `(seconds, attempt, reason) -> Awaitable[None]`.
#
# Why a contextvar: agent_chat wraps with_retry but doesn't itself know
# about the per-cascade-run progress callback. Threading `on_wait`
# through every call site would touch every agent — far simpler to set
# it once at the top of run_cascade and let the deep code grab it.
WAIT_NOTIFIER: contextvars.ContextVar[
    Callable[[float, int, str], Awaitable[None]] | None
] = contextvars.ContextVar("cascade_wait_notifier", default=None)


class RateLimitError(Exception):
    """Raised by claude_cli / llm_client when the underlying provider says
    'come back later'. `retry_after` is the suggested wait in seconds, or
    None if the helper couldn't extract one."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# Patterns: "Resets in 2 hours" / "Try again in 45 minutes" / "retry-after: 60"
# Days are also supported because Claude's weekly-usage caps surface as
# "Resets in 3 days" — the user wants the cascade to just wait for it.
_RESET_RX: list[tuple[re.Pattern, Callable[[re.Match], float]]] = [
    (re.compile(r"reset[s]?\s+in\s+(\d+)\s*days?", re.I), lambda m: float(m.group(1)) * 86400),
    (re.compile(r"reset[s]?\s+in\s+(\d+)\s*hours?", re.I), lambda m: float(m.group(1)) * 3600),
    (re.compile(r"reset[s]?\s+in\s+(\d+)\s*minutes?", re.I), lambda m: float(m.group(1)) * 60),
    (re.compile(r"try\s+again\s+in\s+(\d+)\s*days?", re.I), lambda m: float(m.group(1)) * 86400),
    (re.compile(r"try\s+again\s+in\s+(\d+)\s*hours?", re.I), lambda m: float(m.group(1)) * 3600),
    (re.compile(r"try\s+again\s+in\s+(\d+)\s*minutes?", re.I), lambda m: float(m.group(1)) * 60),
    (re.compile(r"try\s+again\s+in\s+(\d+)\s*seconds?", re.I), lambda m: float(m.group(1))),
    (re.compile(r"retry[\-\s]after[:\s]+(\d+)", re.I), lambda m: float(m.group(1))),
    (re.compile(r"available\s+in\s+(\d+)\s*days?", re.I), lambda m: float(m.group(1)) * 86400),
    (re.compile(r"available\s+in\s+(\d+)\s*hours?", re.I), lambda m: float(m.group(1)) * 3600),
    (re.compile(r"available\s+in\s+(\d+)\s*minutes?", re.I), lambda m: float(m.group(1)) * 60),
]

# Things that look like rate-limit / quota / overload signals
_RL_RX: list[re.Pattern] = [
    re.compile(r"\b429\b"),
    re.compile(r"\b529\b"),
    re.compile(r"\b503\b"),
    re.compile(r"rate[\-_\s]?limit", re.I),
    re.compile(r"too\s+many\s+requests", re.I),
    re.compile(r"usage\s+limit\s+reached", re.I),
    re.compile(r"weekly\s+limit", re.I),
    re.compile(r"approaching\s+usage", re.I),
    re.compile(r"quota\s+exceeded", re.I),
    re.compile(r"overloaded", re.I),
    re.compile(r"session\s+limit", re.I),
    re.compile(r"capacity[_\s]exceeded", re.I),
    re.compile(r"resource[_\s]exhausted", re.I),
    re.compile(r'"type"\s*:\s*"overloaded_error"', re.I),
    re.compile(r'"type"\s*:\s*"rate_limit_error"', re.I),
    # Transient process / network blips — treat like rate-limits so
    # `with_retry` waits + tries again instead of giving up:
    re.compile(r"exited\s+143\b"),                # SIGTERM (process killed externally)
    re.compile(r"exited\s+137\b"),                # SIGKILL / OOM
    re.compile(r"connection\s+(reset|refused|aborted)", re.I),
    re.compile(r"timed\s+out", re.I),
]


def is_rate_limit(text_or_exc: object) -> bool:
    """True if the text/exception looks like a transient rate-limit / quota /
    overload — the kind of error where retrying after a wait makes sense."""
    if text_or_exc is None:
        return False
    text = text_or_exc if isinstance(text_or_exc, str) else str(text_or_exc)
    if not text:
        return False
    return any(p.search(text) for p in _RL_RX)


# Classify *why* the call failed transiently. Some signatures should
# not wait an hour — they're recoverable in seconds (process killed by
# bot restart, network blip, timeout). Returning a small wait here
# bypasses the global `min_backoff_s=3600` floor in with_retry's
# clamp logic.
_SHORT_BACKOFF_RX: list[re.Pattern] = [
    re.compile(r"exited\s+143\b"),                # SIGTERM — bot restart, fix=relaunch
    re.compile(r"exited\s+137\b"),                # SIGKILL / OOM
    re.compile(r"connection\s+(reset|refused|aborted)", re.I),
    re.compile(r"timed\s+out", re.I),
    re.compile(r"timeout", re.I),
    re.compile(r"network\s+is\s+unreachable", re.I),
]


def is_short_backoff_signal(text_or_exc: object) -> bool:
    """True if the transient signal is one we expect to clear quickly
    (10-30s) — SIGTERM/SIGKILL, network blip, plain timeout. Lets
    callers shrink the backoff for those cases instead of treating
    every transient as a 1-hour rate-limit wait."""
    if text_or_exc is None:
        return False
    text = text_or_exc if isinstance(text_or_exc, str) else str(text_or_exc)
    if not text:
        return False
    return any(p.search(text) for p in _SHORT_BACKOFF_RX)


def parse_retry_after(text_or_exc: object) -> float | None:
    """Best-effort: extract a suggested wait (seconds) from the error text.
    Falls back to a SHORT (10s) backoff for known process-kill / network-
    blip signatures — those clear in seconds, not hours."""
    if text_or_exc is None:
        return None
    text = text_or_exc if isinstance(text_or_exc, str) else str(text_or_exc)
    if not text:
        return None
    for rx, extract in _RESET_RX:
        m = rx.search(text)
        if not m:
            continue
        try:
            return float(extract(m))
        except Exception:
            continue
    # No explicit retry-after — but for known short-backoff signatures
    # (SIGTERM, SIGKILL, timeout, connection-reset) suggest 10s instead
    # of letting min_backoff_s=3600 kick in. Bot-restart artifacts
    # absolutely don't need an hour to clear.
    if is_short_backoff_signal(text):
        return 10.0
    return None


async def _wait_with_cancel(
    seconds: float, cancel_event: asyncio.Event | None
) -> bool:
    """Sleep `seconds`, but bail if cancel_event is set. Returns True if
    we were cancelled, False on normal timeout."""
    if cancel_event is None:
        await asyncio.sleep(max(0.0, seconds))
        return False
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=max(0.0, seconds))
        return True
    except asyncio.TimeoutError:
        return False


async def with_retry(
    factory: Callable[[], Awaitable],
    *,
    max_total_wait_s: float = 7 * 86400,
    min_backoff_s: float = 3600.0,
    max_backoff_s: float = 3600.0,
    cancel_event: asyncio.Event | None = None,
    on_wait: Callable[[float, int, str], Awaitable[None]] | None = None,
    label: str = "llm",
):
    """Run `factory()`. On RateLimitError: sleep and retry. Stops when total
    wait exceeds `max_total_wait_s` (re-raises the last RateLimitError) or
    when cancel_event fires (raises asyncio.CancelledError).

    User-explicit policy (2026-04-27): default backoff is **1 hour fixed**
    between retries for ALL Ollama / Claude API / cloud-LLM errors, with a
    7-day total budget — i.e. the cascade keeps trying for up to a week
    until the upstream service recovers. UX-facing callers (triage) override
    these defaults with a tighter budget (10s backoff, 180s total) so the
    chat doesn't freeze when a single triage call blips.
    """
    total_waited = 0.0
    attempt = 0
    while True:
        try:
            return await factory()
        except RateLimitError as e:
            attempt += 1
            wait = e.retry_after
            # Short-backoff bypass: SIGTERM/SIGKILL/timeout/connection-reset
            # signals clear in seconds, not hours. Detect these from the
            # error text and skip the global min_backoff_s=3600 floor —
            # clamp to a tight 10-30s window instead. Without this, every
            # bot-restart artifact (claude -p exited 143) wedges the
            # cascade for a full hour.
            short = is_short_backoff_signal(str(e))
            if wait is None:
                wait = min(max_backoff_s, min_backoff_s * (2 ** (attempt - 1)))
            if short:
                # Tight clamp for fast-recovery signals.
                wait = max(10.0, min(wait, 30.0))
            else:
                wait = max(min_backoff_s, min(wait, max_backoff_s))
            if total_waited + wait > max_total_wait_s:
                log.error(
                    "%s: rate-limit retry budget exhausted after %.0fs total wait; giving up.",
                    label, total_waited,
                )
                raise
            log.warning(
                "%s: rate-limit hit (attempt %d). Waiting %.0fs before retry. Reason: %s",
                label, attempt, wait, str(e)[:200],
            )
            if on_wait is not None:
                try:
                    await on_wait(wait, attempt, str(e)[:200])
                except Exception as cb_err:  # never let the callback abort the retry
                    log.debug("on_wait callback failed: %s", cb_err)
            # Also fire the contextvar-scoped notifier so cascade-runs that
            # set one (run_cascade does) can surface "waiting" status to the
            # user without threading on_wait through every layer.
            ctx_notifier = WAIT_NOTIFIER.get()
            if ctx_notifier is not None and ctx_notifier is not on_wait:
                try:
                    await ctx_notifier(wait, attempt, str(e)[:200])
                except Exception as cb_err:
                    log.debug("ctx wait notifier failed: %s", cb_err)
            cancelled = await _wait_with_cancel(wait, cancel_event)
            if cancelled:
                raise asyncio.CancelledError(f"{label} retry cancelled by user")
            total_waited += wait
