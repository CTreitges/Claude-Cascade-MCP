"""Phase I — Per-Sub-Task-Replan Smoke."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from cascade.agents.planner import SubTask
from cascade.orchestrator import (
    SubTaskResult,
    OrchestratorResult,
    build_replan_feedback,
    collect_replan_targets,
    filter_subtasks_for_replan,
    increment_max_turns_for_retry,
    merge_replan_into_plan,
    transitive_dependents,
)


def passed(label):
    print(f"  ✅ {label}")


def make_result(name, status, **kw):
    r = SubTaskResult(sub_task_name=name, status=status)
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def test_collect_replan_targets():
    print("\n[1] collect_replan_targets")
    res = OrchestratorResult(sub_task_results={
        "explore": make_result("explore", "done"),
        "fix-a": make_result("fix-a", "failed", error="merge conflict"),
        "fix-b": make_result("fix-b", "done"),
        "tests": make_result("tests", "blocked"),
    })
    failed, blocked = collect_replan_targets(res, [])
    assert failed == {"fix-a"}
    assert blocked == {"tests"}
    passed(f"failed={failed}, blocked={blocked}")


def test_transitive_dependents():
    print("\n[2] transitive_dependents")
    sts = [
        SubTask(name="a", summary="…"),
        SubTask(name="b", summary="…", depends_on=["a"]),
        SubTask(name="c", summary="…", depends_on=["b"]),
        SubTask(name="d", summary="…"),
    ]
    deps = transitive_dependents(["a"], sts)
    assert deps == {"b", "c"}, f"got {deps}"
    deps2 = transitive_dependents(["d"], sts)
    assert deps2 == set()
    passed(f"a → {deps}, d → {deps2}")


def test_build_replan_feedback_de():
    print("\n[3] build_replan_feedback (DE)")
    failed = {"fix-a": make_result("fix-a", "failed", error="TypeError: 'int' object is not callable")}
    successful = {"explore": make_result("explore", "done", files_changed=["docs/intro.md"])}
    fb = build_replan_feedback(failed, successful, lang="de")
    assert "fehlgeschlagen" in fb
    assert "TypeError" in fb
    assert "explore" in fb
    assert "docs/intro.md" in fb
    print(fb[:300] + "…")
    passed("DE feedback enthält error + successful files")


def test_filter_subtasks_for_replan():
    print("\n[4] filter_subtasks_for_replan")
    sts = [SubTask(name=n, summary="…") for n in ["a", "b", "c"]]
    out = filter_subtasks_for_replan(sts, {"a", "c"})
    assert [s.name for s in out] == ["b"]
    passed(f"successful={{a,c}}, replan-set={[s.name for s in out]}")


def test_merge_replan_into_plan():
    print("\n[5] merge_replan_into_plan")
    orig = [SubTask(name=n, summary="o") for n in ["a", "b", "c"]]
    new = [SubTask(name="b2", summary="n"), SubTask(name="b3", summary="n")]
    merged = merge_replan_into_plan(orig, {"a", "c"}, new)
    names = [s.name for s in merged]
    assert names == ["a", "c", "b2", "b3"], names
    passed(f"merged: {names}")


def test_increment_max_turns():
    print("\n[6] increment_max_turns_for_retry")
    # Heuristik: error mentioning max_turns → bump
    failed = {"x": make_result("x", "failed", error="max_turns reached")}
    new_max = increment_max_turns_for_retry(failed, base_max_turns=20, increment=10)
    assert new_max == 30
    # Kein max_turns-Hinweis → keine Anpassung
    failed2 = {"x": make_result("x", "failed", error="something else", num_turns=5)}
    no_bump = increment_max_turns_for_retry(failed2, base_max_turns=20)
    assert no_bump == 20
    passed("heuristik bumps bei max_turns-Hinweis, sonst nicht")


def main():
    print("=" * 60)
    print("  Phase I — Replan-Helpers Smoke")
    print("=" * 60)
    test_collect_replan_targets()
    test_transitive_dependents()
    test_build_replan_feedback_de()
    test_filter_subtasks_for_replan()
    test_merge_replan_into_plan()
    test_increment_max_turns()
    print("\n" + "=" * 60)
    print("  ✅ Alle 6 Tests grün")
    print("=" * 60)


if __name__ == "__main__":
    main()
