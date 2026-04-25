"""Implementer: turn a Plan + (optional) reviewer feedback into FileOps via cloud LLM."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from ..claude_cli import parse_json_payload
from ..config import Settings, settings
from ..llm_client import LLMClientError, implementer_chat
from ..workspace import FileOp
from .planner import Plan


class ImplementerOutput(BaseModel):
    ops: list[FileOp] = Field(default_factory=list)
    rationale: str | None = None


IMPLEMENTER_SYSTEM = """You are the Implementer in a three-agent loop
(Planner → YOU → Reviewer). You receive a Plan (and possibly Reviewer feedback
from a failed prior iteration). You produce a JSON object describing the file
operations to apply to the workspace. NEVER include prose outside the JSON,
NEVER use markdown fences, NEVER assume a file exists unless it appears in the
workspace listing.

For "edit" ops, prefer find/replace with a unique 'find' string. If you must
overwrite a file, use op="write" with the full new content.""".strip()


SCHEMA_HINT = """{
  "ops": [
    {"op": "write",  "path": "relative/path.py", "content": "<full file contents>"},
    {"op": "edit",   "path": "relative/path.py", "find": "<unique snippet>", "replace": "<new snippet>"},
    {"op": "delete", "path": "relative/path.py"}
  ],
  "rationale": "<optional one-paragraph explanation>"
}"""


def _build_user_message(
    plan: Plan,
    *,
    workspace_files: list[str],
    feedback: str | None,
    iteration: int,
) -> str:
    parts = [
        f"ITERATION: {iteration}",
        f"\nPLAN SUMMARY:\n{plan.summary}",
        f"\nSTEPS:\n" + "\n".join(f"- {s}" for s in plan.steps),
        f"\nFILES TO TOUCH:\n" + "\n".join(f"- {p}" for p in plan.files_to_touch),
        f"\nACCEPTANCE CRITERIA:\n" + "\n".join(f"- {a}" for a in plan.acceptance_criteria),
        f"\nCURRENT WORKSPACE FILES:\n"
        + ("\n".join(f"- {f}" for f in workspace_files) if workspace_files else "(empty)"),
    ]
    if feedback:
        parts.append(f"\nREVIEWER FEEDBACK FROM PREVIOUS ITERATION (must address):\n{feedback}")
    parts.append("\nRespond with a single JSON object matching the declared schema.")
    return "\n".join(parts)


async def call_implementer(
    plan: Plan,
    *,
    workspace_files: list[str],
    feedback: str | None = None,
    iteration: int = 1,
    model: str | None = None,
    provider: str | None = None,
    s: Settings | None = None,
) -> ImplementerOutput:
    s = s or settings()
    user = _build_user_message(
        plan,
        workspace_files=workspace_files,
        feedback=feedback,
        iteration=iteration,
    )
    reply = await implementer_chat(
        system=IMPLEMENTER_SYSTEM,
        user=user,
        json_schema_hint=SCHEMA_HINT,
        model=model,
        provider=provider,
        s=s,
    )
    return _coerce(reply.text)


def _coerce(raw: str) -> ImplementerOutput:
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError:
        data = parse_json_payload(raw)
    # Some providers return a bare list of ops.
    if isinstance(data, list):
        data = {"ops": data}
    try:
        return ImplementerOutput.model_validate(data)
    except ValidationError as e:
        raise LLMClientError(f"Implementer output failed schema validation: {e}\nraw={raw[:500]!r}") from e
