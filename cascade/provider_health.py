"""Plan v5 R1 — Per-Provider Health-Tracking + Circuit-Breaker.

Vorher (Bug 7+8): jeder LLM-Call ging direkt an den konfigurierten Provider.
Bei 401/auth-error wurde 28× je 1h gewartet (with_retry behandelte 401 als
transient). Bei Provider-Outage gab's keinen Failover.

Jetzt: zentraler ProviderHealth-State mit Circuit-Breaker.
  - permanent errors (401/403/404)         → circuit-open SOFORT, kein retry
  - transient errors (5xx/timeout/network) → counted, after 5 in 5min → open
  - rate-limit (429)                       → counted milder, retry mit backoff
Open circuit blockt weitere Calls für 10min, dann half-open (1 Probe-Call).
Bei Erfolg → closed. Bei Fail → wieder open.

Kein zentraler Service nötig — pro Process eine ProviderHealthRegistry-
Singleton, in-memory. Bei Bot-Restart frischer Start (gewollt: kann sein
dass das Problem zwischenzeitlich gefixt wurde).
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Dict, Literal, Optional


logger = logging.getLogger("cascade.provider_health")


class ErrorKind(Enum):
    PERMANENT = "permanent"      # 401, 403, 404 — niemals self-healing
    RATE_LIMIT = "rate_limit"    # 429 — retry später mit backoff
    TRANSIENT = "transient"      # 5xx, timeout, network — retry sofort
    UNKNOWN = "unknown"          # nicht klassifizierbar


class CircuitState(Enum):
    CLOSED = "closed"        # normal, Calls gehen durch
    OPEN = "open"            # blockiert, Calls werden abgelehnt
    HALF_OPEN = "half_open"  # 1 Probe-Call erlaubt


# ──────────────────────────────────────────────────────────────────────────
#  Error-Klassifikation
# ──────────────────────────────────────────────────────────────────────────
def classify_error(exc: BaseException) -> ErrorKind:
    """Mapped die häufigsten Provider-Errors auf eine Kind."""
    msg = str(exc).lower()
    status = getattr(exc, "status_code", None)

    # HTTP-Status
    if status in (401, 403):
        return ErrorKind.PERMANENT
    if status == 404:
        return ErrorKind.PERMANENT
    if status == 429:
        return ErrorKind.RATE_LIMIT
    if status and 500 <= int(status) < 600:
        return ErrorKind.TRANSIENT

    # Strings als Fallback (für Errors ohne status_code-attr)
    if any(s in msg for s in ("401", "unauthorized", "invalid api key", "invalid_api_key")):
        return ErrorKind.PERMANENT
    if any(s in msg for s in ("403", "forbidden")):
        return ErrorKind.PERMANENT
    if any(s in msg for s in ("404", "not found", "model not found")):
        return ErrorKind.PERMANENT
    if any(s in msg for s in ("429", "rate limit", "rate_limit", "too many requests")):
        return ErrorKind.RATE_LIMIT
    if any(s in msg for s in ("timeout", "timed out", "connection reset", "connection refused", "5xx")):
        return ErrorKind.TRANSIENT
    return ErrorKind.UNKNOWN


# ──────────────────────────────────────────────────────────────────────────
#  Health-State pro Provider
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class ProviderHealth:
    """Per-Provider Circuit-Breaker + Error-Telemetrie."""
    provider: str
    state: CircuitState = CircuitState.CLOSED
    last_error_at: float = 0.0
    last_error_kind: Optional[ErrorKind] = None
    last_error_msg: str = ""
    open_until: float = 0.0           # circuit reopens at this monotonic time
    transient_errors_5min: Deque[float] = field(default_factory=lambda: deque(maxlen=20))
    rate_limit_until: float = 0.0     # für 429-cooldown

    # Tuning
    transient_threshold: int = 5      # 5 Errors / 5min → open
    transient_window_s: float = 300.0
    open_duration_s: float = 600.0    # 10min open dann half-open
    rate_limit_open_duration_s: float = 60.0  # 429 → 1min cooldown vorm Retry

    def record_success(self) -> None:
        """Reset bei erfolgreichem Call."""
        if self.state in (CircuitState.OPEN, CircuitState.HALF_OPEN):
            logger.info(f"provider {self.provider}: circuit CLOSED (success)")
        self.state = CircuitState.CLOSED
        self.transient_errors_5min.clear()
        self.last_error_at = 0.0
        self.last_error_kind = None
        self.last_error_msg = ""

    def record_error(self, exc: BaseException) -> ErrorKind:
        """Notiert einen Error + entscheidet ob Circuit auf-/zumacht."""
        kind = classify_error(exc)
        now = time.monotonic()
        self.last_error_at = now
        self.last_error_kind = kind
        self.last_error_msg = str(exc)[:300]

        if kind == ErrorKind.PERMANENT:
            self.state = CircuitState.OPEN
            self.open_until = now + self.open_duration_s
            logger.warning(
                f"provider {self.provider}: PERMANENT error → circuit OPEN "
                f"for {self.open_duration_s}s ({self.last_error_msg[:80]})"
            )
        elif kind == ErrorKind.RATE_LIMIT:
            self.rate_limit_until = now + self.rate_limit_open_duration_s
            # bleibt CLOSED, aber should_skip prüft rate_limit_until
            logger.info(
                f"provider {self.provider}: rate-limit, cooldown "
                f"{self.rate_limit_open_duration_s}s"
            )
        elif kind == ErrorKind.TRANSIENT:
            self.transient_errors_5min.append(now)
            self._prune_window()
            if len(self.transient_errors_5min) >= self.transient_threshold:
                self.state = CircuitState.OPEN
                self.open_until = now + self.open_duration_s
                logger.warning(
                    f"provider {self.provider}: {len(self.transient_errors_5min)} "
                    f"transient-errors in {self.transient_window_s}s → circuit OPEN"
                )

        return kind

    def should_skip(self) -> bool:
        """True = Provider gerade nicht nutzen."""
        now = time.monotonic()
        if self.rate_limit_until > now:
            return True
        if self.state == CircuitState.CLOSED:
            return False
        if self.state == CircuitState.OPEN:
            if now >= self.open_until:
                self.state = CircuitState.HALF_OPEN
                logger.info(f"provider {self.provider}: circuit HALF_OPEN (probe allowed)")
                return False  # half-open lässt 1 probe durch
            return True
        if self.state == CircuitState.HALF_OPEN:
            return False
        return False

    def _prune_window(self) -> None:
        cutoff = time.monotonic() - self.transient_window_s
        while self.transient_errors_5min and self.transient_errors_5min[0] < cutoff:
            self.transient_errors_5min.popleft()


# ──────────────────────────────────────────────────────────────────────────
#  Registry (Singleton pro Process)
# ──────────────────────────────────────────────────────────────────────────
class _Registry:
    def __init__(self) -> None:
        self._states: Dict[str, ProviderHealth] = {}

    def get(self, provider: str) -> ProviderHealth:
        if provider not in self._states:
            self._states[provider] = ProviderHealth(provider=provider)
        return self._states[provider]

    def reset(self, provider: Optional[str] = None) -> None:
        if provider:
            self._states.pop(provider, None)
        else:
            self._states.clear()

    def snapshot(self) -> Dict[str, Dict]:
        return {
            p: {
                "state": h.state.value,
                "last_error_at": h.last_error_at,
                "last_error_kind": h.last_error_kind.value if h.last_error_kind else None,
                "last_error_msg": h.last_error_msg[:120],
                "transient_errors_5min": len(h.transient_errors_5min),
                "rate_limit_until": h.rate_limit_until,
            }
            for p, h in self._states.items()
        }


_REGISTRY = _Registry()


def get_health(provider: str) -> ProviderHealth:
    return _REGISTRY.get(provider)


def reset_health(provider: Optional[str] = None) -> None:
    _REGISTRY.reset(provider)


def health_snapshot() -> Dict[str, Dict]:
    return _REGISTRY.snapshot()
