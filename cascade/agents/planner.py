"""Planner: turn a free-form task into a structured plan via `claude -p` (Opus)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from ..claude_cli import claude_call, parse_json_payload
from ..config import Settings, settings


class Plan(BaseModel):
    summary: str = Field(..., description="One-paragraph statement of intent.")
    steps: list[str] = Field(default_factory=list)
    files_to_touch: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    notes: str | None = None


PLANNER_SYSTEM = """You are the Planner in a three-agent code-generation loop
(Planner → Implementer → Reviewer). Your only job is to turn the user's task
into a tight, actionable plan that the Implementer can execute. Be concrete:
name files, name functions, name acceptance checks. Do not write code.""".strip()


SCHEMA_HINT = """{
  "summary": "<one-paragraph intent statement>",
  "steps": ["<step 1>", "<step 2>", ...],
  "files_to_touch": ["relative/path.py", ...],
  "acceptance_criteria": ["concrete check 1", "concrete check 2"],
  "notes": "<optional caveats / open questions, or null>"
}"""


def _build_prompt(task: str, recall_context: str | None) -> str:
    parts = [f"TASK:\n{task}"]
    if recall_context:
        parts.append(f"\nRELEVANT MEMORIES:\n{recall_context}")
    parts.append(
        "\nRespond with a single JSON object matching this schema "
        "(no prose, no markdown fences):\n" + SCHEMA_HINT
    )
    return "\n".join(parts)


async def call_planner(
    task: str,
    *,
    attachments: list[Path] | None = None,
    recall_context: str | None = None,
    s: Settings | None = None,
) -> Plan:
    s = s or settings()
    result = await claude_call(
        prompt=_build_prompt(task, recall_context),
        model=s.cascade_planner_model,
        system_prompt=PLANNER_SYSTEM,
        attachments=attachments,
        output_json=True,
    )
    data = parse_json_payload(result.text)
    return Plan.model_validate(data)
