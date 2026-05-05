"""Plan v5 R1 — Failover-Wrapper für LLM-Calls.

Verbindet provider_health mit dem konkreten Call:

  call_with_failover(call_factory, chain) → versucht primary, dann fallbacks
    - überspringt Provider mit open circuit
    - klassifiziert Errors via provider_health.classify_error
    - notiert Erfolg/Fehler in provider_health.record_*

Der `call_factory` ist eine Funktion `(provider, model) -> coroutine`. Die
Failover-Schicht ist provider/model-agnostisch — sie weiß nicht ob's
Anthropic, OpenAI, Ollama ist.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional, Tuple, TypeVar

from cascade.provider_health import (
    ErrorKind,
    classify_error,
    get_health,
)


logger = logging.getLogger("cascade.failover")

T = TypeVar("T")


@dataclass
class FailoverTarget:
    """Ein einzelner (provider, model) im Failover-Chain."""
    provider: str
    model: str
    label: Optional[str] = None  # für Logs

    def __str__(self) -> str:
        return self.label or f"{self.provider}/{self.model}"


@dataclass
class FailoverResult:
    """Ergebnis eines Failover-Runs."""
    success: bool
    used: Optional[FailoverTarget]
    result: Optional[object] = None
    error: Optional[BaseException] = None
    attempts: List[Tuple[FailoverTarget, str]] = None  # (target, error_kind/skip-reason)

    def __post_init__(self):
        if self.attempts is None:
            self.attempts = []


CallFactory = Callable[[FailoverTarget], Awaitable[T]]


async def call_with_failover(
    *,
    call_factory: CallFactory,
    chain: List[FailoverTarget],
    on_failover: Optional[Callable[[FailoverTarget, str], None]] = None,
) -> FailoverResult:
    """Versucht jeden Target in der Chain. Bei permanent-Error → next.
    Bei rate-limit → next (provider-skip greift via should_skip nach
    cooldown). Erfolg → return.

    Returns FailoverResult mit success+used+result oder success=False mit
    aggregiertem error.
    """
    if not chain:
        return FailoverResult(
            success=False, used=None,
            error=ValueError("empty failover chain"),
        )

    result = FailoverResult(success=False, used=None, attempts=[])
    last_error: Optional[BaseException] = None

    for target in chain:
        h = get_health(target.provider)
        if h.should_skip():
            reason = (
                f"circuit-{h.state.value}"
                + (f" (rate-limit until {int(h.rate_limit_until)})"
                   if h.rate_limit_until > 0 else "")
            )
            logger.info(f"failover: skip {target} — {reason}")
            result.attempts.append((target, f"skip:{reason}"))
            if on_failover:
                try:
                    on_failover(target, f"skip:{reason}")
                except Exception:
                    pass
            continue

        try:
            res = await call_factory(target)
            h.record_success()
            result.success = True
            result.used = target
            result.result = res
            result.attempts.append((target, "ok"))
            return result
        except Exception as exc:
            kind = h.record_error(exc)
            last_error = exc
            err_kind_str = kind.value
            logger.warning(
                f"failover: {target} → {err_kind_str} ({str(exc)[:120]})"
            )
            result.attempts.append((target, f"error:{err_kind_str}"))
            if on_failover:
                try:
                    on_failover(target, f"error:{err_kind_str}")
                except Exception:
                    pass

            # Bei TRANSIENT/UNKNOWN: könnte denselben Provider nochmal probieren
            # (mit Backoff), aber das ist Aufgabe von with_retry. Hier gehen
            # wir direkt zum nächsten Target — der Backoff macht der Caller
            # später falls er mit retry wrappt.
            continue

    result.error = last_error
    return result


def build_chain(
    primary_provider: str,
    primary_model: str,
    fallback_specs: List[Tuple[str, str]],
) -> List[FailoverTarget]:
    """Helper: aus (primary, [(prov, model), ...]) die FailoverTarget-Liste."""
    chain = [FailoverTarget(provider=primary_provider, model=primary_model)]
    for prov, model in fallback_specs:
        chain.append(FailoverTarget(provider=prov, model=model))
    # Dedupe — gleiche (provider, model) zwei mal macht keinen Sinn
    seen = set()
    deduped: List[FailoverTarget] = []
    for t in chain:
        key = (t.provider, t.model)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(t)
    return deduped
