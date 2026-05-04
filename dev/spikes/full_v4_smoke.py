"""Plan v4 — Full-Stack End-to-End-Smoke.

Verbindet ALLE Phasen B-I in einem Run:
  - Plan mit 3 Sub-Tasks (DAG: explore → fix-a ‖ fix-b)
  - Orchestrator mit MultiplexedStreamFormatter für Live-Updates
  - Per-Role-Config (Implementer = Sonnet, Reviewer = Sonnet)
  - call_reviewer_via_harness am Ende
  - Replan-Helpers werden auf simulierten Failure-Zustand getestet

Produktions-Flag-Status: alle Feature-Flags default OFF — dieser Test
nutzt die APIs direkt, NICHT via core.py. Phase J's Migration-Hook ist
das setzen von cascade_use_orchestrator/cascade_reviewer_via_harness=True.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from cascade.agents.planner import Plan, SubTask
from cascade.agents.reviewer import call_reviewer_via_harness
from cascade.orchestrator import (
    MultiplexedStreamFormatter,
    Orchestrator,
    SubTaskResult,
    build_replan_feedback,
    collect_replan_targets,
    transitive_dependents,
)
from cascade.role_config import RoleConfig
from cascade.workspace import CheckResult


def shell(cmd, cwd):
    return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)


def setup_repo() -> Path:
    repo = Path(tempfile.mkdtemp(prefix="cascade-v4full-"))
    shell("git init -q -b main", repo)
    shell("git config user.email t@t", repo)
    shell("git config user.name t", repo)
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    (repo / "alpha.py").write_text("def hello():\n    return 'hello'\n")
    (repo / "beta.py").write_text("def world():\n    return 'world'\n")
    shell("git add -A && git commit -q -m base", repo)
    return repo


async def main():
    repo = setup_repo()
    print(f"📁 Repo: {repo}")

    plan = Plan(
        summary="Beide Funktionen sollen Docstrings bekommen.",
        subtasks=[
            SubTask(
                name="explore",
                summary="Welche Funktionen gibt es in welchen Files?",
                acceptance_criteria=["mindestens eine Erkenntnis im final_text"],
            ),
            SubTask(
                name="docs-alpha",
                summary="Docstring zu hello() in alpha.py",
                depends_on=["explore"],
                files_to_touch=["alpha.py"],
                acceptance_criteria=["alpha.py:hello() hat 1-zeiligen Docstring"],
            ),
            SubTask(
                name="docs-beta",
                summary="Docstring zu world() in beta.py",
                depends_on=["explore"],
                files_to_touch=["beta.py"],
                acceptance_criteria=["beta.py:world() hat 1-zeiligen Docstring"],
            ),
        ],
    )

    role = RoleConfig(
        role="implementer",
        harness="claude-code",
        provider="anthropic",
        model="claude-sonnet-4-6",
        max_turns=10,
    )

    fmt = MultiplexedStreamFormatter(lang="de")
    fmt.register_subtasks([s.name for s in plan.subtasks], batches_total=2)

    orch = Orchestrator(
        plan=plan,
        repo_root=repo,
        implementer_role=role,
        max_concurrent=3,
        on_event=fmt.on_event,
        on_subtask_status=fmt.on_status_change,
    )

    print("\n🚀 Orchestrator.run() …\n")
    result = await orch.run()

    print(fmt.render())
    print(f"\n📊 success={result.success} cost=${result.total_cost_usd:.4f} wall={result.total_wall_clock_s:.1f}s")
    for n, r in result.sub_task_results.items():
        print(f"  {n}: {r.status} files={r.files_changed} cost=${r.cost_usd:.4f}")

    # Reviewer-via-Harness am Ende — wenn alle Sub-Tasks erfolgreich waren,
    # holen wir uns einen aggregierten Diff von einem der Branches und
    # lassen den Reviewer drüberschauen.
    if result.success:
        # Pick einer der Sub-Task-Worktrees als Review-Workspace (im
        # produktiven Flow würde man integrierten Diff aus allen Branches bauen)
        first_wt = next((wt for wt in orch.worktree_mgr.list_active()), None)
        if first_wt:
            diff = await orch.worktree_mgr.get_diff_against_base(first_wt.sub_task_id)
            print(f"\n🔍 Reviewer-via-Harness auf {first_wt.sub_task_id} ({len(diff)} chars diff) …")
            review = await call_reviewer_via_harness(
                plan=plan,
                diff=diff[:2000],
                workspace_root=first_wt.path,
                check_results=[CheckResult(name="py-compile", ok=True, exit_code=0, output="", duration_s=0.1)],
                lang="de",
            )
            print(f"   verdict: pass={review.passed} severity={review.severity}")
            print(f"   feedback: {review.feedback[:200]}")

    # Phase-I-Demo: simuliere einen failure und teste replan helpers
    print("\n📋 Phase-I-Demo (synthetischer Failure-Path):")
    fake_result = result
    if fake_result.num_failed == 0:
        # Mache einen artificial Fail um die Helpers zu testen
        if "docs-beta" in fake_result.sub_task_results:
            fake_result.sub_task_results["docs-beta"].status = "failed"
            fake_result.sub_task_results["docs-beta"].error = "simuliert: tests rotgegangen"
    failed, blocked = collect_replan_targets(fake_result, plan.subtasks)
    deps = transitive_dependents(failed, plan.subtasks)
    print(f"   failed: {failed}")
    print(f"   transitively-affected: {deps}")
    fb = build_replan_feedback(
        {n: fake_result.sub_task_results[n] for n in failed},
        {n: r for n, r in fake_result.sub_task_results.items() if r.status == "done"},
        lang="de",
    )
    print(f"   replan-feedback (first 200 chars):\n   {fb[:200]}")

    # Cleanup
    print("\n🧹 cleanup …")
    await orch.cleanup(keep_branches=False)
    shutil.rmtree(repo, ignore_errors=True)
    print("✅ Plan v4 Full-Stack Smoke abgeschlossen.")


if __name__ == "__main__":
    asyncio.run(main())
