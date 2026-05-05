"""Plan v5 R1 — Failover + Provider-Health Smoke-Tests.

Ohne echten LLM-Call — Mock call_factory der gewünschte Errors wirft.
Verifiziert: Circuit-Breaker schließt+öffnet, permanent-Errors triggern
sofort Open, transient-Errors sammeln + Open nach Threshold, Failover
springt zum nächsten Target, Erfolg reset'd alles.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from cascade.failover import FailoverTarget, build_chain, call_with_failover
from cascade.provider_health import (
    CircuitState,
    ErrorKind,
    classify_error,
    get_health,
    health_snapshot,
    reset_health,
)


def passed(label):
    print(f"  ✅ {label}")


def failed(label, why):
    print(f"  ❌ {label}: {why}")
    raise SystemExit(1)


# ──────────────────────────────────────────────────────────────────────
class HTTPError(Exception):
    def __init__(self, msg, status_code=None):
        super().__init__(msg)
        self.status_code = status_code


def test_classify_error():
    print("\n[1] classify_error — alle Klassen")
    assert classify_error(HTTPError("401 unauthorized", status_code=401)) == ErrorKind.PERMANENT
    assert classify_error(HTTPError("forbidden", status_code=403)) == ErrorKind.PERMANENT
    assert classify_error(HTTPError("rate limited", status_code=429)) == ErrorKind.RATE_LIMIT
    assert classify_error(HTTPError("server error", status_code=503)) == ErrorKind.TRANSIENT
    assert classify_error(TimeoutError("connection timed out")) == ErrorKind.TRANSIENT
    # String-Fallback
    assert classify_error(Exception("status code: 401 unauthorized")) == ErrorKind.PERMANENT
    assert classify_error(Exception("Too Many Requests")) == ErrorKind.RATE_LIMIT
    assert classify_error(Exception("connection refused")) == ErrorKind.TRANSIENT
    assert classify_error(Exception("weird non-classifiable")) == ErrorKind.UNKNOWN
    passed("9 Klassifizierungs-Cases")


async def test_permanent_opens_circuit():
    print("\n[2] permanent-error → circuit OPEN sofort")
    reset_health()
    h = get_health("test-provider-A")
    h.record_error(HTTPError("401", status_code=401))
    assert h.state == CircuitState.OPEN
    assert h.should_skip() is True
    passed(f"OPEN nach 1 permanent-error, should_skip=True")


async def test_transient_threshold():
    print("\n[3] transient-error: 5× → OPEN, vorher CLOSED")
    reset_health()
    h = get_health("test-B")
    for i in range(4):
        h.record_error(HTTPError("503", status_code=503))
    assert h.state == CircuitState.CLOSED, "noch unter threshold"
    h.record_error(HTTPError("503", status_code=503))
    assert h.state == CircuitState.OPEN, f"nach 5 transient: {h.state}"
    passed("4× CLOSED, 5× → OPEN")


async def test_success_resets():
    print("\n[4] record_success → circuit CLOSED + window cleared")
    reset_health()
    h = get_health("test-C")
    h.record_error(HTTPError("503", status_code=503))
    h.record_error(HTTPError("503", status_code=503))
    assert len(h.transient_errors_5min) == 2
    h.record_success()
    assert h.state == CircuitState.CLOSED
    assert len(h.transient_errors_5min) == 0
    passed("success räumt window")


async def test_half_open_after_timeout():
    print("\n[5] OPEN → HALF_OPEN nach open_duration_s")
    reset_health()
    h = get_health("test-D")
    h.open_duration_s = 0.1  # 100ms statt 600s für test
    h.record_error(HTTPError("401", status_code=401))
    assert h.state == CircuitState.OPEN
    assert h.should_skip() is True
    await asyncio.sleep(0.15)
    assert h.should_skip() is False, "nach timeout soll half-open sein"
    assert h.state == CircuitState.HALF_OPEN
    passed("OPEN→HALF_OPEN auto-transition")


async def test_failover_skips_open_provider():
    print("\n[6] failover: erste 2 Provider open → 3. greift")
    reset_health()
    # Setup: provider1 + provider2 OPEN, provider3 healthy
    get_health("p1").record_error(HTTPError("401", status_code=401))
    get_health("p2").record_error(HTTPError("401", status_code=401))
    chain = [
        FailoverTarget(provider="p1", model="m1"),
        FailoverTarget(provider="p2", model="m2"),
        FailoverTarget(provider="p3", model="m3"),
    ]
    async def factory(t):
        if t.provider == "p3":
            return f"ok from {t}"
        raise HTTPError("should never be called")
    res = await call_with_failover(call_factory=factory, chain=chain)
    assert res.success, f"sollte success sein, attempts={res.attempts}"
    assert res.used.provider == "p3"
    assert res.result == "ok from p3/m3"
    # attempts: erste 2 als skip, 3. als ok
    assert res.attempts[0][1].startswith("skip:"), res.attempts
    assert res.attempts[1][1].startswith("skip:"), res.attempts
    assert res.attempts[2][1] == "ok"
    passed(f"failover {len(res.attempts)} attempts → succeed @ {res.used}")


async def test_failover_all_fail():
    print("\n[7] failover: alle fail → success=False")
    reset_health()
    chain = [
        FailoverTarget(provider="x1", model="m"),
        FailoverTarget(provider="x2", model="m"),
    ]
    async def factory(t):
        raise HTTPError("401 unauthorized", status_code=401)
    res = await call_with_failover(call_factory=factory, chain=chain)
    assert not res.success
    assert res.error is not None
    assert all(a[1].startswith("error:") for a in res.attempts)
    passed("alle fail → success=False, alle attempts marked")


async def test_failover_first_succeeds():
    print("\n[8] failover: primary klappt → kein fallback")
    reset_health()
    chain = [
        FailoverTarget(provider="ok1", model="m"),
        FailoverTarget(provider="ok2", model="m"),
    ]
    calls = []
    async def factory(t):
        calls.append(t.provider)
        return "ok"
    res = await call_with_failover(call_factory=factory, chain=chain)
    assert res.success
    assert res.used.provider == "ok1"
    assert calls == ["ok1"], f"factory sollte nur 1× rufen, war {calls}"
    passed("primary ok → no fallback")


async def test_build_chain_dedupe():
    print("\n[9] build_chain dedupe")
    chain = build_chain(
        primary_provider="anthropic", primary_model="claude-sonnet-4-6",
        fallback_specs=[
            ("anthropic", "claude-sonnet-4-6"),  # duplicate of primary
            ("ollama", "kimi-k2.6"),
            ("anthropic", "claude-sonnet-4-6"),  # duplicate again
        ],
    )
    assert len(chain) == 2, f"expected 2 (after dedupe), got {[str(t) for t in chain]}"
    passed(f"dedupe → {[str(t) for t in chain]}")


async def main():
    print("=" * 60)
    print("  Plan v5 R1 — Failover + Provider-Health Smoke")
    print("=" * 60)
    test_classify_error()
    await test_permanent_opens_circuit()
    await test_transient_threshold()
    await test_success_resets()
    await test_half_open_after_timeout()
    await test_failover_skips_open_provider()
    await test_failover_all_fail()
    await test_failover_first_succeeds()
    await test_build_chain_dedupe()
    print("\n" + "=" * 60)
    print("  ✅ Alle 9 Tests grün")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
