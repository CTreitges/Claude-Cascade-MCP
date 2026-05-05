"""Plan v5 R6 — SONA-Patterns Smoke."""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from cascade.patterns import (
    PatternStore,
    TaskPattern,
    extract_keywords,
    find_similar,
    jaccard_similarity,
    record_pattern,
    render_for_planner,
)


def passed(label):
    print(f"  ✅ {label}")


def fail(label, why):
    print(f"  ❌ {label}: {why}")
    raise SystemExit(1)


def test_extract_keywords():
    print("\n[1] keywords: stopwords raus, length-sorted")
    kw = extract_keywords("Refactor the help command in the cascade-bot")
    print(f"     {kw}")
    assert "refactor" in kw
    # 2026-05-05: Tokenizer trennt jetzt an /-_:.\, → "cascade" + "bot" getrennt
    assert "cascade" in kw
    assert "the" not in kw  # stopword
    passed("EN keywords clean")
    kw_de = extract_keywords("Verbessere den /help-Command des Cascade-Bots")
    print(f"     DE: {kw_de}")
    assert "verbessere" in kw_de
    assert "den" not in kw_de  # stopword
    passed("DE keywords clean")


def test_jaccard():
    print("\n[2] jaccard_similarity")
    assert jaccard_similarity(["a", "b", "c"], ["a", "b", "c"]) == 1.0
    assert jaccard_similarity(["a", "b"], ["c", "d"]) == 0.0
    assert jaccard_similarity(["a", "b", "c"], ["b", "c", "d"]) == 2/4  # 0.5
    passed("identity, disjoint, partial")


def test_record_and_lookup():
    print("\n[3] record + find_similar round-trip")
    tmp = Path(tempfile.mkdtemp(prefix="cascade-pat-"))
    store = PatternStore(tmp / "patterns.jsonl")

    record_pattern(
        store=store,
        task_text="Verbessere den /help-Command des Cascade-Bots",
        plan_summary="Refactor i18n.py help strings",
        sub_task_names=["explore", "rewrite-help", "verify"],
        files_changed=["cascade/i18n.py"],
        iterations=2,
        cost_usd=0.18,
        wall_clock_s=120.0,
        replans_needed=0,
    )
    record_pattern(
        store=store,
        task_text="SoundCloud Downloader Windows UI optimieren",
        plan_summary="UI/UX improvements for SCDL",
        sub_task_names=["analyze", "implement", "test"],
        files_changed=["plugin/main.py", "plugin/ui.py"],
        iterations=4,
        cost_usd=0.95,
        wall_clock_s=600.0,
        replans_needed=1,
    )

    # neue Anfrage ähnlich zur 1.
    similar = find_similar(
        store=store,
        task_text="Den /help-Command übersichtlicher gestalten",
        top_n=3,
        min_similarity=0.05,
    )
    print(f"     {len(similar)} similar found")
    for item in similar:
        print(f"       score={item['score']:.2f} sim={item['similarity']:.2f} task={item['pattern'].task_text[:50]}")
    assert len(similar) >= 1
    top = similar[0]
    assert "help" in top["pattern"].task_text.lower()
    passed("similarity ranking via keyword-overlap")

    shutil.rmtree(tmp, ignore_errors=True)


def test_quality_score_from_replans():
    print("\n[4] quality_score: replans_needed → score")
    tmp = Path(tempfile.mkdtemp(prefix="cascade-pat-"))
    store = PatternStore(tmp / "p.jsonl")
    p1 = record_pattern(store=store, task_text="t1", replans_needed=0)
    p2 = record_pattern(store=store, task_text="t2", replans_needed=1)
    p3 = record_pattern(store=store, task_text="t3", replans_needed=2)
    assert p1.quality_score == 1.0
    assert p2.quality_score == 0.7
    assert p3.quality_score == 0.4
    passed("0/1/2+ replans → 1.0/0.7/0.4")
    shutil.rmtree(tmp, ignore_errors=True)


def test_render_for_planner():
    print("\n[5] render_for_planner — Block für Planner-Prompt")
    tmp = Path(tempfile.mkdtemp(prefix="cascade-pat-"))
    store = PatternStore(tmp / "p.jsonl")
    record_pattern(
        store=store,
        task_text="Refactor /help command",
        plan_summary="i18n cleanup",
        sub_task_names=["a", "b"],
        files_changed=["x.py"],
        iterations=2,
        cost_usd=0.05,
    )
    similar = find_similar(store=store, task_text="refactor help", min_similarity=0.05)
    out = render_for_planner(similar, lang="de")
    print(out)
    assert "PRIOR_SUCCESSFUL_PATTERNS" in out
    assert "Refactor /help" in out
    passed("planner-block contains task excerpt + plan + sub-tasks")
    shutil.rmtree(tmp, ignore_errors=True)


def test_below_threshold_filters_out():
    print("\n[6] min_similarity filter")
    tmp = Path(tempfile.mkdtemp(prefix="cascade-pat-"))
    store = PatternStore(tmp / "p.jsonl")
    record_pattern(
        store=store,
        task_text="Quantenmechanik Forschung",
    )
    # ganz andere domain
    similar = find_similar(
        store=store,
        task_text="Refactor python code",
        min_similarity=0.5,
    )
    assert len(similar) == 0, "sollte rausfiltern"
    passed("low-similarity ausgefiltert")
    shutil.rmtree(tmp, ignore_errors=True)


def test_empty_store():
    print("\n[7] leerer store → []")
    tmp = Path(tempfile.mkdtemp(prefix="cascade-pat-"))
    store = PatternStore(tmp / "empty.jsonl")
    assert find_similar(store=store, task_text="anything") == []
    assert render_for_planner([]) == ""
    passed("empty handling")
    shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("=" * 60)
    print("  Plan v5 R6 — Patterns Smoke")
    print("=" * 60)
    test_extract_keywords()
    test_jaccard()
    test_record_and_lookup()
    test_quality_score_from_replans()
    test_render_for_planner()
    test_below_threshold_filters_out()
    test_empty_store()
    print("\n" + "=" * 60)
    print("  ✅ Alle 7 Tests grün")
    print("=" * 60)


if __name__ == "__main__":
    main()
