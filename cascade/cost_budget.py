"""Plan v5 R3 — Cost-Budget + Warnings.

Inspiration: Ruflo's cost-tracker. Per-Run-Budget + Per-Day-Cap. Pre-Call-
Check estimiert die Kosten und blockt/degraded wenn Budget gesprengt
würde. Telegram-Warnings bei 50%/80%/95%-Schwellen.

Reuse cascade/pricing.py (Phase F) für USD-Berechnung. Token-Estimation
für Pre-Call via tiktoken-Approximation auf Prompt-Länge.

DB: cost_history-Table (rolling 30 Tage) für Per-Day/Per-Month-Caps.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from cascade.pricing import compute_cost, get_model_price


logger = logging.getLogger("cascade.cost_budget")


@dataclass
class BudgetState:
    """Aktueller Budget-Verbrauch eines Runs."""
    run_id: str
    spent_usd: float = 0.0
    by_role: Dict[str, float] = field(default_factory=dict)
    by_model: Dict[str, float] = field(default_factory=dict)
    warnings_emitted: List[float] = field(default_factory=list)  # 0.5/0.8/0.95
    started_at: float = field(default_factory=time.time)

    def add_call(self, role: str, model: str, usd: float) -> None:
        self.spent_usd += usd
        self.by_role[role] = self.by_role.get(role, 0.0) + usd
        self.by_model[model] = self.by_model.get(model, 0.0) + usd


@dataclass
class BudgetLimits:
    """Per-Run / Per-Day / Per-Month Caps."""
    per_run_max_usd: float = 5.0       # default $5/Task
    per_day_max_usd: float = 50.0      # default $50/Tag
    per_month_max_usd: float = 1000.0  # default $1000/Monat
    warn_thresholds: Tuple[float, ...] = (0.5, 0.8, 0.95)


class BudgetExceededError(Exception):
    """Wird vor einem Call geraised wenn Budget greift."""
    def __init__(self, scope: str, current: float, limit: float):
        super().__init__(
            f"Budget {scope} exceeded: ${current:.4f} / ${limit:.2f}"
        )
        self.scope = scope
        self.current = current
        self.limit = limit


# ──────────────────────────────────────────────────────────────────────
#  Token-Estimation (für Pre-Call-Check)
# ──────────────────────────────────────────────────────────────────────
def estimate_tokens(text: str) -> int:
    """Grobe Token-Schätzung — chars/4 als Faustregel.

    Genauer wäre tiktoken (für OpenAI) oder Anthropic-spezifischer
    Tokenizer, aber chars/4 ist innerhalb 20% Genauigkeit für Englisch
    und für Pre-Call-Budget-Check absolut ausreichend.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


def estimate_call_cost(
    *,
    prompt_text: str,
    expected_output_tokens: int = 1000,
    model: str,
) -> float:
    """USD-Vorhersage für einen einzelnen LLM-Call."""
    prompt_tokens = estimate_tokens(prompt_text)
    price = get_model_price(model)
    return price.cost_for(
        input_tokens=prompt_tokens,
        output_tokens=expected_output_tokens,
    )


# ──────────────────────────────────────────────────────────────────────
#  Budget-Checks
# ──────────────────────────────────────────────────────────────────────
def check_pre_call(
    state: BudgetState,
    limits: BudgetLimits,
    estimated_call_usd: float,
    *,
    day_spent_usd: float = 0.0,
    month_spent_usd: float = 0.0,
) -> Optional[BudgetExceededError]:
    """Vor einem Call: würde der das Budget sprengen?

    Returns None wenn OK, sonst BudgetExceededError-Instanz (caller
    entscheidet ob raise oder degrade).
    """
    if state.spent_usd + estimated_call_usd > limits.per_run_max_usd:
        return BudgetExceededError(
            "per-run",
            state.spent_usd + estimated_call_usd,
            limits.per_run_max_usd,
        )
    if day_spent_usd + estimated_call_usd > limits.per_day_max_usd:
        return BudgetExceededError(
            "per-day",
            day_spent_usd + estimated_call_usd,
            limits.per_day_max_usd,
        )
    if month_spent_usd + estimated_call_usd > limits.per_month_max_usd:
        return BudgetExceededError(
            "per-month",
            month_spent_usd + estimated_call_usd,
            limits.per_month_max_usd,
        )
    return None


def check_warnings(
    state: BudgetState,
    limits: BudgetLimits,
) -> List[Tuple[float, str]]:
    """Liefert neue Warnungen die noch nicht emittiert wurden.

    Format: [(threshold_fraction, human_message), ...]
    """
    out: List[Tuple[float, str]] = []
    for t in limits.warn_thresholds:
        if t in state.warnings_emitted:
            continue
        if state.spent_usd >= t * limits.per_run_max_usd:
            state.warnings_emitted.append(t)
            pct = int(t * 100)
            out.append((
                t,
                f"⚠️ Budget-Warnung: Run-Cost ${state.spent_usd:.4f} "
                f"({pct}% von ${limits.per_run_max_usd:.2f} Limit)",
            ))
    return out


def degrade_model_for_budget(
    *,
    state: BudgetState,
    limits: BudgetLimits,
    current_model: str,
    role: str,
) -> Optional[str]:
    """Bei Budget-Sprenge: schlage billigeres Modell vor.

    Heuristik:
      - Opus → Sonnet
      - Sonnet → Haiku
      - alles andere → None (kein Downgrade)
    """
    cur = current_model.lower()
    if "opus" in cur:
        return "claude-sonnet-4-6"
    if "sonnet" in cur:
        return "claude-haiku-4-5"
    return None


# ──────────────────────────────────────────────────────────────────────
#  Recording (für post-call Bookkeeping)
# ──────────────────────────────────────────────────────────────────────
def record_call(
    state: BudgetState,
    *,
    role: str,
    model: str,
    actual_usage: Optional[dict] = None,
    actual_usd: Optional[float] = None,
) -> float:
    """Notiert einen erfolgten Call. Returns die abgerechnete USD-Summe.

    Bevorzugt actual_usd, fällt auf actual_usage (dict mit input/output_tokens)
    + pricing.compute_cost zurück, sonst 0.
    """
    if actual_usd is not None:
        usd = float(actual_usd)
    elif actual_usage is not None:
        usd = compute_cost(actual_usage, model)
    else:
        usd = 0.0
    state.add_call(role, model, usd)
    return usd
