"""Plan v4 Phase E — Orchestrator + Worktree-Manager + Parallel-Scheduler.

Verbindet die Foundation-Phasen B (Harness), C (RoleConfig), D (DAG) zu
einem orchestrierten Multi-Sub-Task-Runner mit:

  - git worktree pro Sub-Task (Workspace-Isolation)
  - Topological Batches → parallel-Execution per Batch (asyncio.gather +
    Semaphore)
  - Pro-Sub-Task Stream-Events (für Phase H: Multiplexed Telegram)
  - SubTaskResult mit files_changed + tool_calls + cost
  - Failure-Isolation: ein failed Sub-Task blockiert nur die transitiv
    abhängigen, nicht den ganzen Plan
  - Per-Sub-Task-Replan-Hook (Phase I)

Wird von core.py via Feature-Flag `cascade_use_orchestrator` opt-in
genutzt (Phase J). Solange das Flag false ist, läuft die existierende
sub-task-Schleife unverändert weiter.
"""
from cascade.orchestrator.result import (
    OrchestratorResult,
    SubTaskResult,
)
from cascade.orchestrator.replan import (
    build_replan_feedback,
    collect_replan_targets,
    filter_subtasks_for_replan,
    increment_max_turns_for_retry,
    merge_replan_into_plan,
    transitive_dependents,
)
from cascade.orchestrator.scheduler import Orchestrator
from cascade.orchestrator.telegram_format import MultiplexedStreamFormatter
from cascade.orchestrator.worktree import Worktree, WorktreeManager
from cascade.orchestrator.runner import run_subtask_via_harness

__all__ = [
    "OrchestratorResult",
    "SubTaskResult",
    "Orchestrator",
    "Worktree",
    "WorktreeManager",
    "run_subtask_via_harness",
    "MultiplexedStreamFormatter",
    "build_replan_feedback",
    "collect_replan_targets",
    "filter_subtasks_for_replan",
    "increment_max_turns_for_retry",
    "merge_replan_into_plan",
    "transitive_dependents",
]
