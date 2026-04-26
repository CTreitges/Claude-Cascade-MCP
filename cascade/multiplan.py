"""Multi-plan voting: ask the planner twice (different temperatures) and
let Sonnet pick the better plan before the loop starts.

Doubles the planner LLM cost so it's gated behind
`Settings.cascade_multiplan_enabled`. Per-chat toggle exists but is off
by default — only worth it for ambitious tasks where plan quality is
the bottleneck.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .agents.planner import Plan, call_planner
from .claude_cli import parse_json_payload
from .config import Settings, settings
from .llm_client import LLMClientError, agent_chat

log = logging.getLogger("cascade.multiplan")


_PICK_SYSTEM_DE = """Du bekommst zwei Pläne für dieselbe Aufgabe und musst
EINEN auswählen. Bewerte:
  - Vollständigkeit (deckt der Plan alle Akzeptanzkriterien ab?)
  - Konkretheit (Datei-Namen + Funktions-Namen statt Floskeln?)
  - Quality-Checks (objektiv, scriptbar, deckt das Wesentliche?)
  - Sub-Task-Schnitt (sinnvoll dekomponiert oder unnötig zerlegt?)

Antworte AUSSCHLIESSLICH mit JSON, kein Markdown:
  {"winner": 1 | 2, "reason": "<knapper Satz>"}"""


_PICK_SYSTEM_EN = """You get two plans for the same task and must pick ONE.
Judge by:
  - Completeness (does the plan cover every acceptance criterion?)
  - Concreteness (file names + function names, not platitudes?)
  - Quality checks (objective, scriptable, covers what matters?)
  - Sub-task layout (sensible decomposition or over-split?)

Reply with JSON ONLY, no markdown:
  {"winner": 1 | 2, "reason": "<short sentence>"}"""


def _summarize_plan(p: Plan, label: int) -> str:
    sub_names = [st.name for st in (p.subtasks or [])][:6]
    qc_names = [c.name for c in (p.quality_checks or [])][:6]
    return (
        f"PLAN {label}:\n"
        f"  summary: {p.summary[:300]}\n"
        f"  steps ({len(p.steps)}): {', '.join((p.steps or [])[:5])[:300]}\n"
        f"  files_to_touch: {', '.join(p.files_to_touch[:6])}\n"
        f"  acceptance ({len(p.acceptance_criteria)}): "
        f"{'; '.join((p.acceptance_criteria or [])[:4])[:300]}\n"
        f"  quality_checks: {', '.join(qc_names)}\n"
        f"  subtasks: {', '.join(sub_names)}\n"
    )


async def call_planner_multi(
    task: str,
    *,
    attachments: list[Path] | None = None,
    recall_context: str | None = None,
    repo_candidates_block: str | None = None,
    replan_feedback: str | None = None,
    external_context: str | None = None,
    base_temperature: float | None = None,
    lang: str = "en",
    s: Settings | None = None,
) -> tuple[Plan, str]:
    """Run two planner calls in parallel with different temperatures, then
    pick the better one. Returns (winning_plan, picker_reason).

    On any failure (one planner crashes, picker can't decide), falls back
    cleanly to whichever plan succeeded — preserving the single-plan path.
    """
    s = s or settings()
    t1 = base_temperature if base_temperature is not None else 0.2
    t2 = 0.7  # higher → more diverse alternative
    p1_task = call_planner(
        task,
        attachments=attachments,
        recall_context=recall_context,
        repo_candidates_block=repo_candidates_block,
        replan_feedback=replan_feedback,
        external_context=external_context,
        temperature=t1,
        lang=lang,
        s=s,
    )
    p2_task = call_planner(
        task,
        attachments=attachments,
        recall_context=recall_context,
        repo_candidates_block=repo_candidates_block,
        replan_feedback=replan_feedback,
        external_context=external_context,
        temperature=t2,
        lang=lang,
        s=s,
    )
    p1, p2 = await asyncio.gather(p1_task, p2_task, return_exceptions=True)

    # Recover from partial failure
    if isinstance(p1, Exception) and isinstance(p2, Exception):
        # Both crashed — re-raise the first; caller's existing error handling kicks in.
        raise p1
    if isinstance(p1, Exception):
        return (p2, "fallback: plan 1 crashed, plan 2 used")
    if isinstance(p2, Exception):
        return (p1, "fallback: plan 2 crashed, plan 1 used")

    # Ask the picker
    pick_prompt = (
        f"TASK:\n{task[:600]}\n\n"
        + _summarize_plan(p1, 1) + "\n"
        + _summarize_plan(p2, 2)
    )
    try:
        raw = await agent_chat(
            prompt=pick_prompt,
            model=s.cascade_reviewer_model,
            system_prompt=_PICK_SYSTEM_DE if lang == "de" else _PICK_SYSTEM_EN,
            output_json=True,
            timeout_s=90,
            retry_max_total_wait_s=180.0,
            retry_min_backoff_s=10.0,
            s=s,
        )
        data = parse_json_payload(raw)
        winner = int(data.get("winner") or 0)
        reason = str(data.get("reason") or "").strip()[:200]
    except (LLMClientError, Exception) as e:
        log.warning("multiplan picker failed: %s — defaulting to plan 1", e)
        return (p1, "picker unreachable; defaulted to plan 1")

    if winner == 2:
        return (p2, reason or "plan 2 chosen")
    # Default to plan 1 on missing/invalid winner
    return (p1, reason or "plan 1 chosen")
