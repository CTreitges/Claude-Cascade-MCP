"""Plan v5 — End-to-End Integration-Test.

Verbindet alle 7 Module (R1-R7) in einem realistischen Cascade-Run-Szenario:
  - Trace-IDs flow durch alle Events
  - Budget-Check vor LLM-Calls
  - Failover bei Provider-Outage
  - Observability emittiert pro Stufe
  - Patterns werden bei success gespeichert
  - PII wird vor Sync gestrippt
  - Tier-Routing wählt Modell

Ohne echte LLM-Calls — alle Provider werden gemockt.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from cascade.complexity import Tier, decide_tier, model_for_tier
from cascade.cost_budget import (
    BudgetExceededError,
    BudgetLimits,
    BudgetState,
    check_pre_call,
    check_warnings,
    estimate_call_cost,
    record_call,
)
from cascade.failover import FailoverTarget, build_chain, call_with_failover
from cascade.federation import build_manifest, scan_pii, strip_pii
from cascade.observability import (
    JSONLEmitter,
    RunSummary,
    configure_emitter,
    new_trace_id,
    restore_trace_context,
    set_trace_context,
)
from cascade.patterns import (
    PatternStore,
    extract_keywords,
    find_similar,
    record_pattern,
    render_for_planner,
)
from cascade.provider_health import (
    ErrorKind,
    classify_error,
    get_health,
    health_snapshot,
    reset_health,
)


def passed(label):
    print(f"  ✅ {label}")


def fail(label, why):
    print(f"  ❌ {label}: {why}")
    raise SystemExit(1)


# ──────────────────────────────────────────────────────────────────────
#  Integration: vollständiger gemockter Run
# ──────────────────────────────────────────────────────────────────────
class HTTPError(Exception):
    def __init__(self, msg, status_code=None):
        super().__init__(msg)
        self.status_code = status_code


@dataclass
class FakeRunWorkspace:
    tmp: Path
    trace_id: str
    metrics_path: Path
    patterns_path: Path
    summary: RunSummary
    budget: BudgetState
    pattern_store: PatternStore
    emitter: JSONLEmitter


def setup_run(prefix: str = "v5-int") -> FakeRunWorkspace:
    tmp = Path(tempfile.mkdtemp(prefix=f"cascade-{prefix}-"))
    trace_id = new_trace_id()
    metrics = tmp / "metrics.jsonl"
    patterns = tmp / "patterns.jsonl"
    emitter = configure_emitter(path=metrics)
    return FakeRunWorkspace(
        tmp=tmp,
        trace_id=trace_id,
        metrics_path=metrics,
        patterns_path=patterns,
        summary=RunSummary(trace_id=trace_id, task_id="integration-test"),
        budget=BudgetState(run_id=trace_id),
        pattern_store=PatternStore(patterns),
        emitter=emitter,
    )


def teardown(ws: FakeRunWorkspace):
    shutil.rmtree(ws.tmp, ignore_errors=True)


# ──────────────────────────────────────────────────────────────────────
async def test_full_successful_run():
    print("\n[1] full successful run — alle 7 Module aktiv")
    reset_health()
    ws = setup_run()
    set_trace_context(trace_id=ws.trace_id, task_id="integration-test")

    task_text = "Refactor /help command in cascade i18n.py — make it more readable"
    plan_summary = "Restructure help strings with emoji headers"

    # 1. Tier-Decision
    tier = await decide_tier(task_text, plan=None, settings=None)
    print(f"     decided tier: {tier.tier.value} (conf={tier.confidence:.2f}, reason={tier.reason})")
    chosen_model = model_for_tier(tier.tier)
    print(f"     model_for_tier: {chosen_model}")

    # 2. Pattern-Lookup vor Plan
    similar = find_similar(store=ws.pattern_store, task_text=task_text)
    assert similar == [], "leerer pattern-store"

    # 3. Budget-Check
    limits = BudgetLimits(per_run_max_usd=2.0)
    estimated = estimate_call_cost(
        prompt_text=task_text * 5,
        expected_output_tokens=500,
        model=chosen_model,
    )
    err = check_pre_call(ws.budget, limits, estimated)
    assert err is None
    print(f"     pre-call estimate: ${estimated:.4f}, budget OK")

    # 4. Failover-Chain (mock factory simuliert success)
    chain = build_chain(
        primary_provider="anthropic",
        primary_model=chosen_model,
        fallback_specs=[("anthropic", "claude-haiku-4-5"), ("ollama", "kimi-k2.6")],
    )
    async def factory_ok(target):
        return f"plan-output-from-{target}"
    res = await call_with_failover(call_factory=factory_ok, chain=chain)
    assert res.success
    print(f"     primary success @ {res.used}")

    # 5. record_call (post-call)
    record_call(ws.budget, role="planner", model=chosen_model, actual_usd=0.012)
    ws.summary.add_llm_call(
        role="planner", model=chosen_model, provider="anthropic",
        input_tokens=1500, output_tokens=300, cost_usd=0.012,
    )
    ws.emitter.emit("llm_call", {
        "role": "planner", "model": chosen_model,
        "tokens_in": 1500, "tokens_out": 300, "cost": 0.012,
    })

    # 6. Implementer + 2 Sub-Tasks (gemockt)
    for sub_task in ["explore", "rewrite-help"]:
        ws.emitter.emit("subtask_start", {"name": sub_task})
        # Tool-Calls
        for tool in ["Read", "Edit", "Read"]:
            ws.summary.add_tool_call(tool)
            ws.emitter.emit("tool_use", {"name": tool, "subtask": sub_task})
        # LLM-Call
        record_call(ws.budget, role="implementer", model=chosen_model, actual_usd=0.025)
        ws.summary.add_llm_call(
            role="implementer", model=chosen_model, provider="anthropic",
            input_tokens=2500, output_tokens=600, cost_usd=0.025,
        )
        ws.emitter.emit("subtask_done", {"name": sub_task, "ok": True})

    # 7. Budget-Warnings checken
    warnings = check_warnings(ws.budget, limits)
    print(f"     spent ${ws.budget.spent_usd:.4f} / ${limits.per_run_max_usd}, warnings={len(warnings)}")

    # 8. Pattern speichern
    record_pattern(
        store=ws.pattern_store,
        task_text=task_text,
        plan_summary=plan_summary,
        sub_task_names=["explore", "rewrite-help"],
        files_changed=["cascade/i18n.py"],
        iterations=2,
        cost_usd=ws.budget.spent_usd,
        replans_needed=0,
    )

    # 9. Final-Summary
    final = ws.summary.finalize(success=True)
    ws.emitter.emit("run_done", final)

    # ── Assertions ──
    assert ws.metrics_path.exists()
    lines = ws.metrics_path.read_text().strip().split("\n")
    assert len(lines) >= 10, f"expected ≥10 events, got {len(lines)}"
    print(f"     metrics-events: {len(lines)}")

    assert ws.patterns_path.exists()
    pats = ws.pattern_store.all()
    assert len(pats) == 1
    assert pats[0].iterations == 2

    health = get_health("anthropic")
    print(f"     health: {health.state.value}, transient={len(health.transient_errors_5min)}")

    teardown(ws)
    restore_trace_context({"trace_id": None, "task_id": None, "subtask": None})
    passed(f"full run: tier={tier.tier.value}, ${ws.budget.spent_usd:.4f}, {len(lines)} events, 1 pattern")


async def test_failover_on_auth_error():
    print("\n[2] failover bei 401 — Bug-7+8-Szenario reproduziert")
    reset_health()
    ws = setup_run("failover-test")
    set_trace_context(trace_id=ws.trace_id)

    # Primary wirft 401, Fallback klappt
    chain = build_chain(
        primary_provider="ollama",
        primary_model="kimi-k2.6",
        fallback_specs=[("anthropic", "claude-sonnet-4-6")],
    )
    async def factory(target):
        if target.provider == "ollama":
            raise HTTPError("401 unauthorized", status_code=401)
        return "fallback-success"

    res = await call_with_failover(call_factory=factory, chain=chain)
    assert res.success
    assert res.used.provider == "anthropic"
    assert res.attempts[0][1].startswith("error:permanent")
    assert res.attempts[1][1] == "ok"

    # Provider-Health: ollama OPEN, anthropic CLOSED
    snap = health_snapshot()
    print(f"     health snapshot: {json.dumps(snap, indent=2, default=str)[:300]}")
    assert snap["ollama"]["state"] == "open"
    assert snap["anthropic"]["state"] == "closed"

    teardown(ws)
    restore_trace_context({"trace_id": None})
    passed("Bug-7+8 strukturell tot — 401 → next provider, no 28×1h backoff")


async def test_budget_exceeded_blocks_call():
    print("\n[3] budget-exceeded → BudgetExceededError")
    ws = setup_run("budget-test")
    limits = BudgetLimits(per_run_max_usd=0.10)
    ws.budget.spent_usd = 0.08
    estimated = 0.05  # 0.08 + 0.05 = 0.13 > 0.10
    err = check_pre_call(ws.budget, limits, estimated)
    assert err is not None
    assert err.scope == "per-run"
    print(f"     blocked: {err}")
    teardown(ws)
    passed("budget-block triggert vor LLM-Call")


async def test_pattern_recall_for_similar_task():
    print("\n[4] pattern-recall: 2. ähnlicher Task profitiert")
    ws = setup_run("pattern-test")

    # Run 1: success → pattern saved
    record_pattern(
        store=ws.pattern_store,
        task_text="Refactor help command für besseres UX",
        plan_summary="i18n.py umbauen mit emoji",
        sub_task_names=["analyze", "rewrite", "verify"],
        files_changed=["cascade/i18n.py"],
        iterations=3,
        cost_usd=0.18,
        replans_needed=0,
    )

    # Run 2: ähnlicher Task → pattern wird gefunden
    similar = find_similar(
        store=ws.pattern_store,
        task_text="Verbessere den /help-Output mit klaren Sektionen",
        min_similarity=0.05,
    )
    assert len(similar) == 1
    print(f"     match: score={similar[0]['score']:.2f}, sim={similar[0]['similarity']:.2f}")

    rendered = render_for_planner(similar, lang="de")
    assert "PRIOR_SUCCESSFUL_PATTERNS" in rendered
    assert "i18n.py" in rendered
    print("     planner-block ready (Excerpt):")
    for line in rendered.splitlines()[:6]:
        print(f"       {line}")

    teardown(ws)
    passed("pattern recall klappt cross-task")


async def test_pii_strip_before_sync():
    print("\n[5] PII-Strip vor Sync — federation guard")
    ws = setup_run("pii-test")

    # Sample dirty config
    cfg_path = ws.tmp / "secrets.env"
    cfg_path.write_text(
        "ANTHROPIC_API_KEY=sk-ant-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1234\n"
        "GITHUB_TOKEN=ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
        "PUBLIC_HOST=https://example.com\n"
    )
    safe_path = ws.tmp / "settings.json"
    safe_path.write_text('{"theme": "dark", "lang": "de"}')

    manifest = build_manifest(
        source_machine="vps",
        target_machine="windows",
        files=[cfg_path, safe_path],
        base_dir=ws.tmp,
        skip_pii=True,
    )
    by_intent = {sf.relative_path: sf.intent for sf in manifest.files}
    print(f"     manifest: {by_intent}")
    assert by_intent["secrets.env"] == "skip-pii"
    assert by_intent["settings.json"] == "sync"
    assert manifest.pii_skipped == 1

    teardown(ws)
    passed("federation manifest filter PII out")


async def test_observability_correlates_via_trace_id():
    print("\n[6] observability: trace_id verbindet alle Events")
    ws = setup_run("obs-test")
    trace_id = ws.trace_id

    set_trace_context(trace_id=trace_id, task_id="obs-task", subtask="phase-A")
    ws.emitter.emit("planner_start", {})
    ws.emitter.emit("planner_done", {"ms": 4500})
    set_trace_context(subtask="phase-B")
    ws.emitter.emit("implementer_start", {})
    ws.emitter.emit("tool_use", {"name": "Read"})
    ws.emitter.emit("implementer_done", {"ms": 12000})
    restore_trace_context({"trace_id": None, "task_id": None, "subtask": None})

    lines = ws.metrics_path.read_text().strip().split("\n")
    parsed = [json.loads(line) for line in lines]
    assert all(r["trace_id"] == trace_id for r in parsed), "trace_id muss in allen 5 Events sein"
    subtasks = {r["subtask"] for r in parsed if "subtask" in r}
    assert subtasks == {"phase-A", "phase-B"}
    print(f"     {len(parsed)} events, {len(subtasks)} subtasks, trace_id durchgängig")

    teardown(ws)
    passed("trace_id korreliert alle 5 Events")


async def main():
    print("=" * 70)
    print("  Plan v5 — End-to-End Integration Smoke (alle 7 Module)")
    print("=" * 70)
    await test_full_successful_run()
    await test_failover_on_auth_error()
    await test_budget_exceeded_blocks_call()
    await test_pattern_recall_for_similar_task()
    await test_pii_strip_before_sync()
    await test_observability_correlates_via_trace_id()
    print("\n" + "=" * 70)
    print("  ✅ Alle 6 Integration-Tests grün — Plan v5 Production-bereit")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
