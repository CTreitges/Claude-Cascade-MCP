"""Reviewer: check Implementer's diff against Plan via `claude -p` (Sonnet)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..claude_cli import claude_call, parse_json_payload
from ..config import Settings, settings
from .planner import Plan


class ReviewResult(BaseModel):
    passed: bool = Field(..., alias="pass")
    feedback: str = ""
    failing_criteria: list[str] = Field(default_factory=list)
    severity: str = "low"  # low | medium | high — used for RLM decision

    model_config = {"populate_by_name": True}


REVIEWER_SYSTEM = """You are the Reviewer in a three-agent loop. Given the
original Plan and the Implementer's git diff, decide whether the change
satisfies every acceptance criterion. Be strict but actionable: if you fail
the iteration, explain *what specifically* the Implementer must change in
the next iteration. Reply ONLY with a JSON object — no markdown, no prose.""".strip()


SCHEMA_HINT = """{
  "pass": true | false,
  "feedback": "<empty if pass=true; otherwise concrete instructions for the next iteration>",
  "failing_criteria": ["criterion text", ...],
  "severity": "low" | "medium" | "high"
}"""


def _build_prompt(plan: Plan, diff: str) -> str:
    return (
        f"PLAN:\nsummary: {plan.summary}\n"
        f"steps:\n" + "\n".join(f"- {s}" for s in plan.steps) + "\n"
        f"acceptance_criteria:\n" + "\n".join(f"- {a}" for a in plan.acceptance_criteria) + "\n"
        f"\nDIFF:\n{diff or '(empty diff — implementer produced no changes)'}\n"
        "\nRespond with a single JSON object matching this schema:\n" + SCHEMA_HINT
    )


async def call_reviewer(
    plan: Plan,
    diff: str,
    *,
    s: Settings | None = None,
) -> ReviewResult:
    s = s or settings()
    result = await claude_call(
        prompt=_build_prompt(plan, diff),
        model=s.cascade_reviewer_model,
        system_prompt=REVIEWER_SYSTEM,
        output_json=True,
    )
    data = parse_json_payload(result.text)
    return ReviewResult.model_validate(data)
