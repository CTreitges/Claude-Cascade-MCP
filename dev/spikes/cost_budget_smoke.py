"""Plan v5 R3 — Cost-Budget Smoke."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from cascade.cost_budget import (
    BudgetExceededError,
    BudgetLimits,
    BudgetState,
    check_pre_call,
    check_warnings,
    degrade_model_for_budget,
    estimate_call_cost,
    estimate_tokens,
    record_call,
)


def passed(label):
    print(f"  ✅ {label}")


def fail(label, why):
    print(f"  ❌ {label}: {why}")
    raise SystemExit(1)


def test_estimate_tokens():
    print("\n[1] estimate_tokens — chars/4")
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello world") == 11 // 4  # 2
    assert estimate_tokens("a" * 4000) == 1000
    passed("scaling chars/4")


def test_estimate_call_cost():
    print("\n[2] estimate_call_cost — Sonnet vs Opus")
    cost_sonnet = estimate_call_cost(prompt_text="x" * 4000, expected_output_tokens=500, model="claude-sonnet-4-6")
    cost_opus = estimate_call_cost(prompt_text="x" * 4000, expected_output_tokens=500, model="claude-opus-4-7")
    assert cost_sonnet < cost_opus
    print(f"     sonnet=${cost_sonnet:.4f}, opus=${cost_opus:.4f}")
    passed("opus deutlich teurer als sonnet")


def test_pre_call_within_budget():
    print("\n[3] pre-call innerhalb Budget → None")
    state = BudgetState(run_id="t1", spent_usd=2.0)
    limits = BudgetLimits(per_run_max_usd=5.0)
    err = check_pre_call(state, limits, estimated_call_usd=0.5)
    assert err is None
    passed("$2 + $0.5 vs $5 Limit → OK")


def test_pre_call_per_run_blocks():
    print("\n[4] pre-call sprengt per-run-Limit")
    state = BudgetState(run_id="t2", spent_usd=4.5)
    limits = BudgetLimits(per_run_max_usd=5.0)
    err = check_pre_call(state, limits, estimated_call_usd=1.0)
    assert err is not None
    assert err.scope == "per-run"
    print(f"     Error: {err}")
    passed("per-run-Limit detected")


def test_pre_call_per_day_blocks():
    print("\n[5] pre-call sprengt per-day-Limit")
    state = BudgetState(run_id="t3", spent_usd=0.5)
    limits = BudgetLimits(per_run_max_usd=10.0, per_day_max_usd=20.0)
    err = check_pre_call(state, limits, estimated_call_usd=2.0, day_spent_usd=19.0)
    assert err is not None
    assert err.scope == "per-day"
    passed("per-day-Limit detected")


def test_warnings_emit_at_thresholds():
    print("\n[6] Warnungen bei 50/80/95%")
    state = BudgetState(run_id="t4")
    limits = BudgetLimits(per_run_max_usd=10.0, warn_thresholds=(0.5, 0.8, 0.95))
    state.spent_usd = 2.0
    assert len(check_warnings(state, limits)) == 0
    state.spent_usd = 5.0  # 50%
    w1 = check_warnings(state, limits)
    assert len(w1) == 1 and w1[0][0] == 0.5
    print(f"     50%: {w1[0][1]}")
    # nochmaliger check liefert nicht erneut die 50%-Warnung
    assert len(check_warnings(state, limits)) == 0
    state.spent_usd = 8.5  # 85% → triggert 80% threshold
    w2 = check_warnings(state, limits)
    assert len(w2) == 1 and w2[0][0] == 0.8
    state.spent_usd = 9.6  # 96%
    w3 = check_warnings(state, limits)
    assert len(w3) == 1 and w3[0][0] == 0.95
    passed("alle 3 Schwellen einmal-emit")


def test_degrade_model():
    print("\n[7] degrade_model Opus→Sonnet→Haiku")
    state = BudgetState(run_id="t5")
    limits = BudgetLimits()
    assert degrade_model_for_budget(
        state=state, limits=limits, current_model="claude-opus-4-7", role="implementer",
    ) == "claude-sonnet-4-6"
    assert degrade_model_for_budget(
        state=state, limits=limits, current_model="claude-sonnet-4-6", role="implementer",
    ) == "claude-haiku-4-5"
    assert degrade_model_for_budget(
        state=state, limits=limits, current_model="kimi-k2.6", role="implementer",
    ) is None
    passed("opus→sonnet→haiku, ollama→None")


def test_record_call_actual_usd():
    print("\n[8] record_call mit actual_usd")
    state = BudgetState(run_id="t6")
    record_call(state, role="planner", model="claude-opus-4-7", actual_usd=0.05)
    record_call(state, role="implementer", model="claude-sonnet-4-6", actual_usd=0.02)
    assert state.spent_usd == 0.07
    assert state.by_role == {"planner": 0.05, "implementer": 0.02}
    assert state.by_model == {"claude-opus-4-7": 0.05, "claude-sonnet-4-6": 0.02}
    passed(f"by_role + by_model akkumuliert: ${state.spent_usd:.4f}")


def test_record_call_via_usage_dict():
    print("\n[9] record_call mit usage-dict")
    state = BudgetState(run_id="t7")
    usage = {"input_tokens": 1000, "output_tokens": 500}
    usd = record_call(state, role="implementer", model="claude-opus-4-7", actual_usage=usage)
    # 1000*15/1M + 500*75/1M = 0.015 + 0.0375 = 0.0525
    assert abs(usd - 0.0525) < 1e-6
    print(f"     opus 1k+500 → ${usd:.4f}")
    passed("via usage-dict fall-back auf pricing.compute_cost")


def main():
    print("=" * 60)
    print("  Plan v5 R3 — Cost-Budget Smoke")
    print("=" * 60)
    test_estimate_tokens()
    test_estimate_call_cost()
    test_pre_call_within_budget()
    test_pre_call_per_run_blocks()
    test_pre_call_per_day_blocks()
    test_warnings_emit_at_thresholds()
    test_degrade_model()
    test_record_call_actual_usd()
    test_record_call_via_usage_dict()
    print("\n" + "=" * 60)
    print("  ✅ Alle 9 Tests grün")
    print("=" * 60)


if __name__ == "__main__":
    main()
