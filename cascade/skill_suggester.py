"""Auto-skill suggester: after a successful cascade run, ask the planner
model whether the recent task pattern is worth turning into a reusable skill.

Skills are reusable task templates with {placeholders} the user can re-fill
later. They live in store.skills and are surfaced via /skills in the bot.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import BaseModel, Field

from .claude_cli import claude_call, parse_json_payload
from .config import Settings, settings

log = logging.getLogger("cascade.skill_suggester")


class SkillSuggestion(BaseModel):
    should_create: bool = Field(..., description="True only if a real, reusable pattern emerged.")
    name: str | None = None  # snake_case, ≤30 chars
    description: str | None = None  # one short sentence
    task_template: str | None = None  # the new task with {placeholders}
    placeholders: list[str] = Field(default_factory=list)
    rationale: str | None = None  # why this is worth a skill (or why not)


SYSTEM_PROMPT = """You are the skill-curator for a small coding bot.
Your job: look at recent successful tasks and decide whether they form a
reusable PATTERN worth saving as a parametrised skill.

A skill is worthwhile ONLY if:
- The same kind of task has happened ≥2 times (or is clearly setup for repeats), AND
- Most of the wording is stable across runs, AND
- The varying parts can be cleanly captured as {placeholders}.

Examples of GOOD skills:
- "Erstelle pytest-Tests für {file} mit Fokus auf {aspect}"
- "Im Repo {repo} eine README erweitern um den Abschnitt {section}"

Examples of BAD skills (do NOT save):
- One-off greenfield tasks ("Erstelle hello.py")
- Long, very specific tasks where every word matters
- Tasks that are easier just typed out fresh

Output STRICT JSON only — no markdown fences, no prose. Schema:
{
  "should_create": true | false,
  "name": "<snake_case, ≤30 chars, English>" | null,
  "description": "<one short sentence>" | null,
  "task_template": "<the templated task with {placeholders}>" | null,
  "placeholders": ["<name1>", "<name2>"],
  "rationale": "<why or why not, 1 sentence>"
}

If should_create=false, set name/description/task_template to null and
explain in `rationale` (e.g. "single occurrence, no clear pattern yet")."""


def _format_recent_tasks(tasks: list[Any]) -> str:
    if not tasks:
        return "(none)"
    lines = []
    for t in tasks:
        lines.append(
            f"- id={t.id} status={t.status} created={int(t.created_at)} task={(t.task_text or '')[:200]!r}"
        )
    return "\n".join(lines)


async def maybe_suggest_skill(
    *,
    current_task,
    recent_tasks: list,
    existing_skills: list[dict] | None = None,
    s: Settings | None = None,
    cooldown_s: float = 300,
    last_suggested_at: float | None = None,
) -> SkillSuggestion | None:
    """Ask Opus whether to create a skill. Returns None on cooldown / disabled
    / when the call fails.
    """
    s = s or settings()

    # Don't spam suggestions
    if last_suggested_at and (time.time() - last_suggested_at) < cooldown_s:
        return None
    if len(recent_tasks) < 2:
        return None  # need ≥2 successful tasks to detect a pattern

    skip_names = {sk["name"] for sk in (existing_skills or [])}
    skip_block = (
        "\nALREADY-SAVED SKILLS (don't propose these names again):\n"
        + "\n".join(f"- {n}" for n in skip_names)
    ) if skip_names else ""

    prompt = (
        f"CURRENT TASK (just completed):\n  id={current_task.id} task={(current_task.task_text or '')[:300]!r}\n"
        f"\nRECENT SUCCESSFUL TASKS (most recent first):\n{_format_recent_tasks(recent_tasks)}"
        f"{skip_block}\n"
        "\nDecide whether to save a reusable skill now. Respond with JSON only."
    )

    try:
        result = await claude_call(
            prompt=prompt,
            model=s.cascade_planner_model,
            system_prompt=SYSTEM_PROMPT,
            output_json=True,
            timeout_s=120,
            effort=s.cascade_planner_effort or None,
        )
    except Exception as e:
        log.warning("skill_suggester claude call failed: %s", e)
        return None

    try:
        data = parse_json_payload(result.text)
        sug = SkillSuggestion.model_validate(data)
    except Exception as e:
        log.warning("skill_suggester parse failed: %s — text=%s", e, result.text[:300])
        return None

    if not sug.should_create:
        return None
    if not sug.name or not sug.task_template:
        return None
    return sug


def format_skill_proposal(sug: SkillSuggestion, lang: str = "de") -> str:
    if lang == "de":
        return (
            "💡 *Skill-Vorschlag*\n\n"
            f"*Name:* `{sug.name}`\n"
            f"*Beschreibung:* {sug.description or '—'}\n"
            f"*Vorlage:*\n```\n{sug.task_template}\n```\n"
            f"*Platzhalter:* {', '.join(sug.placeholders) or '—'}\n"
            f"*Begründung:* {sug.rationale or '—'}\n"
        )
    return (
        "💡 *Skill suggestion*\n\n"
        f"*Name:* `{sug.name}`\n"
        f"*Description:* {sug.description or '—'}\n"
        f"*Template:*\n```\n{sug.task_template}\n```\n"
        f"*Placeholders:* {', '.join(sug.placeholders) or '—'}\n"
        f"*Rationale:* {sug.rationale or '—'}\n"
    )
