"""Phase I — Per-Sub-Task-Replan.

Wenn ein Sub-Task in einem Orchestrator-Run failed, will man typisch
NICHT den ganzen Plan neu starten. Stattdessen:

  1. Die Sub-Task-Branches der erfolgreichen Sub-Tasks BEHALTEN
  2. Den failed Sub-Task neu planen mit Kontext
     - was die anderen erreicht haben
     - was schief lief (error / reviewer feedback)
  3. Optional: anderes Modell oder mehr max_turns für den Re-Run
  4. Die transitiv abhängigen Sub-Tasks (vorher "blocked") mit dem
     neuen Sub-Task neu durchlaufen

Diese Datei liefert die Hilfsfunktionen — der Aufruf liegt beim Caller
(typisch core.py in Phase J, oder direkt im Bot).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from cascade.orchestrator.result import OrchestratorResult, SubTaskResult


logger = logging.getLogger("cascade.orchestrator.replan")


def collect_replan_targets(
    result: OrchestratorResult,
    all_subtasks: Iterable[Any],
) -> Tuple[Set[str], Set[str]]:
    """Bestimmt welche Sub-Tasks neu laufen müssen.

    Returns:
        (failed_to_replan, dependents_to_rerun)
        - failed_to_replan: Sub-Task-Names mit status="failed"
        - dependents_to_rerun: Sub-Task-Names die transitiv blocked waren
                                (status="blocked" / "skipped" durch failed-deps)
    """
    failed = {n for n, r in result.sub_task_results.items() if r.status == "failed"}
    blocked = {n for n, r in result.sub_task_results.items() if r.status in ("blocked", "skipped")}
    return failed, blocked


def transitive_dependents(
    target_names: Iterable[str],
    all_subtasks: Iterable[Any],
) -> Set[str]:
    """Findet alle Sub-Tasks die transitiv von einer der target_names abhängen."""
    target_set = set(target_names)
    affected = set(target_set)
    changed = True
    by_name = {st.name: st for st in all_subtasks}
    while changed:
        changed = False
        for name, st in by_name.items():
            if name in affected:
                continue
            if any(dep in affected for dep in st.depends_on):
                affected.add(name)
                changed = True
    return affected - target_set


def build_replan_feedback(
    failed_results: Dict[str, SubTaskResult],
    successful_results: Dict[str, SubTaskResult],
    lang: str = "de",
) -> str:
    """Generiert ein menschenlesbares Replan-Feedback für den Planner.

    Wird typischerweise als `replan_feedback` an `call_planner_replan`
    weitergereicht — gibt dem Planner Kontext WAS schief lief und WAS
    schon funktioniert.
    """
    lines: List[str] = []
    if lang == "de":
        if failed_results:
            lines.append("Folgende Sub-Tasks sind fehlgeschlagen und brauchen einen neuen Plan:")
            for name, r in failed_results.items():
                err = (r.error or "—").splitlines()[0][:160]
                lines.append(f"- **{name}**: {err}")
                if r.tool_calls:
                    last_tools = ", ".join(set(tc.get("name", "") for tc in r.tool_calls[-5:]))
                    lines.append(f"  letzte Tools: {last_tools}")
        if successful_results:
            lines.append("\nBereits erfolgreiche Sub-Tasks (deren Ergebnisse nicht erneut gebraucht werden):")
            for name, r in successful_results.items():
                files = ", ".join(r.files_changed[:5]) or "—"
                lines.append(f"- **{name}** (✅): geänderte Files: {files}")
        lines.append(
            "\nErstelle einen NEUEN Plan, der die fehlgeschlagenen Sub-Tasks ersetzt — "
            "evtl. mit anderem Schritt-Ablauf, anderen files_to_touch oder kleineren "
            "Akzeptanzkriterien. Die erfolgreichen Sub-Tasks NICHT neu planen."
        )
    else:
        if failed_results:
            lines.append("The following sub-tasks failed and need a new plan:")
            for name, r in failed_results.items():
                err = (r.error or "—").splitlines()[0][:160]
                lines.append(f"- **{name}**: {err}")
                if r.tool_calls:
                    last_tools = ", ".join(set(tc.get("name", "") for tc in r.tool_calls[-5:]))
                    lines.append(f"  last tools: {last_tools}")
        if successful_results:
            lines.append("\nAlready-successful sub-tasks (no re-plan needed):")
            for name, r in successful_results.items():
                files = ", ".join(r.files_changed[:5]) or "—"
                lines.append(f"- **{name}** (✅): files: {files}")
        lines.append(
            "\nGenerate a NEW plan that REPLACES the failed sub-tasks only — "
            "maybe with different steps, files_to_touch, or smaller acceptance "
            "criteria. Keep the successful ones as-is."
        )
    return "\n".join(lines)


def filter_subtasks_for_replan(
    all_subtasks: List[Any],
    successful_names: Set[str],
) -> List[Any]:
    """Liefert nur die Sub-Tasks zurück die neu geplant werden müssen
    (= alles was nicht erfolgreich war), für einen partiellen Replan."""
    return [st for st in all_subtasks if st.name not in successful_names]


def merge_replan_into_plan(
    original_subtasks: List[Any],
    successful_names: Set[str],
    new_subtasks: List[Any],
) -> List[Any]:
    """Kombiniert die erfolgreichen Original-Sub-Tasks mit den neu-geplanten.

    Erfolgreiche Sub-Tasks bleiben; failed/blocked werden durch die
    new_subtasks ersetzt. Reihenfolge: zuerst erfolgreiche (in original-
    Reihenfolge), dann neue. Caller-Pflicht: depends_on-Refs in
    new_subtasks zeigen sinnvoll auf erfolgreiche Originale oder
    untereinander.
    """
    successful = [st for st in original_subtasks if st.name in successful_names]
    return successful + list(new_subtasks)


def increment_max_turns_for_retry(
    failed_results: Dict[str, SubTaskResult],
    base_max_turns: int = 20,
    increment: int = 10,
    cap: int = 60,
) -> int:
    """Wenn ein Sub-Task wegen max_turns-Erschöpfung failed, kann ein Replan
    mit höherem Budget Sinn machen. Heuristik: lookups bei was die Errors
    waren, und gibt eine empfohlene neue max_turns zurück."""
    if not failed_results:
        return base_max_turns
    needs_more = any(
        "max_turns" in (r.error or "").lower()
        or r.num_turns >= base_max_turns - 1
        for r in failed_results.values()
    )
    if needs_more:
        return min(cap, base_max_turns + increment)
    return base_max_turns
