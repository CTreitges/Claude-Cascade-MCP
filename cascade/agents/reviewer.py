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


def _format_check_results(results) -> str:
    if not results:
        return "(none defined)"
    lines = []
    for r in results:
        mark = "✅" if r.ok else "❌"
        out_excerpt = (r.output or "").strip().splitlines()
        tail = (out_excerpt[-3:] if out_excerpt else [])
        lines.append(
            f"{mark} {r.name} (exit={r.exit_code}, {r.duration_s:.1f}s)\n"
            + ("\n".join(f"    | {l[:200]}" for l in tail) if tail else "")
        )
    return "\n".join(lines)


def _build_prompt(plan: Plan, diff: str, check_results=None) -> str:
    parts = [
        f"PLAN:\nsummary: {plan.summary}",
        f"steps:\n" + "\n".join(f"- {s}" for s in plan.steps),
        f"acceptance_criteria:\n" + "\n".join(f"- {a}" for a in plan.acceptance_criteria),
    ]
    if check_results is not None:
        parts.append(f"\nQUALITY CHECK RESULTS:\n{_format_check_results(check_results)}")
        parts.append(
            "Rule: if ANY check failed (❌), you MUST set pass=false and explain "
            "exactly which check needs to pass and how the implementer should fix it."
        )
    parts.append(f"\nDIFF:\n{diff or '(empty diff — implementer produced no changes)'}")
    parts.append("\nRespond with a single JSON object matching this schema:\n" + SCHEMA_HINT)
    return "\n".join(parts)


async def call_reviewer(
    plan: Plan,
    diff: str,
    *,
    check_results=None,
    s: Settings | None = None,
) -> ReviewResult:
    s = s or settings()
    result = await claude_call(
        prompt=_build_prompt(plan, diff, check_results),
        model=s.cascade_reviewer_model,
        system_prompt=REVIEWER_SYSTEM,
        output_json=True,
        effort=s.cascade_reviewer_effort or None,
    )
    data = parse_json_payload(result.text)
    return ReviewResult.model_validate(data)
