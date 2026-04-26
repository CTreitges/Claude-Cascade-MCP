"""Post-run self-reflection: ask Sonnet what could have been better and
persist the answer as an RLM finding so future similar tasks see it.

Triggered by `core.run_cascade` after a successful (`status='done'`) run.
The next planner call automatically picks the lesson up via BM25 recall —
no extra wiring needed.
"""

from __future__ import annotations

import logging

from .agents.planner import Plan
from .config import Settings, settings
from .llm_client import LLMClientError, agent_chat
from .memory import remember_finding

log = logging.getLogger("cascade.reflect")


_SYSTEM_DE = """Du bist der Self-Critic eines Coding-Bots. Eine Cascade ist
gerade erfolgreich durchgelaufen. Schau auf Plan + Iterationen + finalen
Diff und nenne konkret 1-3 Verbesserungspunkte, die KÜNFTIGE ähnliche
Tasks effizienter machen würden:

  - Welche Quality-Checks hätten früher gegriffen?
  - Welcher Plan-Schritt war redundant oder fehlte?
  - Welcher Implementer-Hinweis hätte eine Iteration gespart?
  - Welche Datei-Konvention hätte vorab klargestellt sein sollen?

KEIN Lob, KEINE Wiederholung des Tasks. Nur die Lessons als knappe
Bullet-Liste, max. 3 Punkte, jeder ≤ 1 Satz. Wenn nichts substantielles
zu sagen ist, antworte mit GENAU `nothing-to-add` (Lowercase).
Format: nur die Bulletpoints, kein Header, kein Markdown."""


_SYSTEM_EN = """You are a coding bot's self-critic. A cascade just finished
successfully. Look at plan + iterations + final diff and name 1-3 concrete
improvements that would speed up FUTURE similar tasks:

  - which quality-check should have caught the issue earlier?
  - which plan step was redundant or missing?
  - which implementer hint would have saved an iteration?
  - which file convention should have been explicit upfront?

NO praise, NO restating the task. Just the lessons as a short bullet list,
max 3 points, each ≤ 1 sentence. If there is nothing meaningful to say,
reply with EXACTLY `nothing-to-add` (lowercase).
Format: just the bullets, no header, no markdown."""


async def reflect_on_run(
    *,
    task: str,
    plan: Plan,
    iterations: int,
    final_diff: str,
    s: Settings | None = None,
    lang: str = "en",
) -> str | None:
    """Returns the lesson text (or None on `nothing-to-add` / LLM failure).
    Caller is responsible for persisting via `persist_lessons`.
    """
    s = s or settings()
    plan_block = (
        f"PLAN:\n  summary: {plan.summary}\n"
        "  steps:\n" + "\n".join(f"    - {st}" for st in plan.steps[:8]) + "\n"
        "  acceptance:\n" + "\n".join(f"    - {a}" for a in plan.acceptance_criteria[:6])
    )
    diff_excerpt = final_diff or "(empty)"
    if len(diff_excerpt) > 6000:
        diff_excerpt = diff_excerpt[:6000] + "\n…[truncated]"
    prompt = (
        f"TASK:\n{task[:800]}\n\n"
        f"{plan_block}\n\n"
        f"ITERATIONS_USED: {iterations}\n\n"
        f"FINAL_DIFF:\n{diff_excerpt}"
    )
    try:
        raw = await agent_chat(
            prompt=prompt,
            model=s.cascade_reviewer_model,
            system_prompt=_SYSTEM_DE if lang == "de" else _SYSTEM_EN,
            output_json=False,
            timeout_s=120,
            retry_max_total_wait_s=180.0,
            retry_min_backoff_s=10.0,
            s=s,
        )
    except LLMClientError as e:
        log.warning("reflect llm failed: %s", e)
        return None
    text = (raw or "").strip()
    if not text or text.lower().startswith("nothing-to-add"):
        return None
    return text


async def persist_lessons(text: str, *, task_id: str, task_text: str) -> None:
    """Save lessons into RLM. Tags include the task summary keywords so
    BM25 recall surfaces them for similar future tasks."""
    # Cheap keyword-extraction from the task — first 6 alnum words ≥4 chars.
    keywords = []
    cur = []
    for ch in task_text.lower():
        if ch.isalnum():
            cur.append(ch)
        else:
            if cur:
                w = "".join(cur)
                cur = []
                if len(w) >= 4:
                    keywords.append(w)
                    if len(keywords) >= 6:
                        break
    if cur:
        w = "".join(cur)
        if len(w) >= 4:
            keywords.append(w)
    tag_keywords = ",".join(keywords[:6]) or "general"
    await remember_finding(
        f"[lessons-learned task={task_id}]\n{text}",
        category="finding",
        importance="medium",
        tags=f"claude-cascade,lessons-learned,{tag_keywords}",
    )
