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


IMPLEMENTER_SYSTEM = r"""You are the Implementer in a three-agent loop
(Planner → YOU → Reviewer). You receive a Plan (and possibly Reviewer feedback
from a failed prior iteration). You produce a JSON object describing the file
operations to apply to the workspace. NEVER include prose outside the JSON,
NEVER use markdown fences, NEVER assume a file exists unless it appears in the
workspace listing.

CRITICAL — YOU CANNOT READ FILES. There is NO `read` op. Files in the workspace
that matter for this iteration ARE ALREADY in your prompt below under
`EXISTING FILE CONTENTS`. Treat that section as your read-buffer:
  - For audit / refactor tasks: open `EXISTING FILE CONTENTS`, identify what
    needs changing, then emit `op=write` (full new file body) or `op=edit`
    (find/replace) ops directly. DO NOT ask for file contents — they're
    already there.
  - If a file you need is NOT in `EXISTING FILE CONTENTS`, mention it in
    `rationale` so the next planner round can pull it in. Then still emit
    whatever ops you CAN do safely with what you have.
  - NEVER respond with empty `ops: []` and a rationale of "I cannot proceed
    without read access" — that's a guaranteed dead-end. Use the file
    contents in your prompt.

For "edit" ops, prefer find/replace with a unique 'find' string. If you must
overwrite a file, use op="write" with the full new content.

When a previous iteration's ops were rejected (visible as `OP FAILURES` in the
feedback below), READ those failure messages — they tell you exactly what
went wrong (path outside workspace, find-string-not-unique, syntax error,
stub function detected, etc.). Adjust accordingly; do NOT just re-emit the
same op.

CRITICAL — DO NOT WRITE TO GENERATED ARTIFACTS. Files like `*.pyc`, `*.pyo`,
`__pycache__/...`, `.so`, `node_modules/...`, `dist/`, `build/`, `.next/`,
`.venv/`, `venv/` are produced by the toolchain. Editing them does NOTHING
to the source — the runner will reject those ops and the reviewer will keep
saying "source not modified". ALWAYS target the source file
(e.g. `telegram/handler.py`, never `telegram/__pycache__/handler.cpython-312.pyc`).

CRITICAL — STRING ESCAPING IN FILE CONTENT. The `content` field is a JSON
string, but its VALUE is the literal file content. To write a regex like
`re.match(r'^https?://(www\.|m\.)?…')` into a Python file, the JSON value
must contain a SINGLE backslash before the dot, not double. In JSON that's
written `"\\."`  — but the resulting file content is `\.` (one backslash).
NEVER write `"\\\\."` (which puts `\\.` into the file — a regex bug). When
in doubt, use op="write" with the full file body and verify mentally that
each `\` you intend would need a single `\\` in JSON, not `\\\\`.""".strip()


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
    file_contents: dict[str, str] | None = None,
    external_context: str | None = None,
) -> str:
    parts = [
        f"ITERATION: {iteration}",
        f"\nPLAN SUMMARY:\n{plan.summary}",
        "\nSTEPS:\n" + "\n".join(f"- {s}" for s in plan.steps),
        "\nFILES TO TOUCH:\n" + "\n".join(f"- {p}" for p in plan.files_to_touch),
        "\nACCEPTANCE CRITERIA:\n" + "\n".join(f"- {a}" for a in plan.acceptance_criteria),
        "\nCURRENT WORKSPACE FILES:\n"
        + ("\n".join(f"- {f}" for f in workspace_files) if workspace_files else "(empty)"),
    ]
    if external_context:
        parts.append(f"\n{external_context}")
    if file_contents:
        parts.append(
            "\nEXISTING FILE CONTENTS (read-only context — these files already exist "
            "in the workspace; modify them via op=edit/write or use them as reference):"
        )
        for path, body in file_contents.items():
            parts.append(f"\n--- BEGIN {path} ---\n{body}\n--- END {path} ---")
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
    effort: str | None = None,
    temperature: float | None = None,
    external_context: str | None = None,
    s: Settings | None = None,
    file_contents: dict[str, str] | None = None,
) -> ImplementerOutput:
    s = s or settings()
    user = _build_user_message(
        plan,
        workspace_files=workspace_files,
        feedback=feedback,
        iteration=iteration,
        file_contents=file_contents,
        external_context=external_context,
    )
    reply = await implementer_chat(
        system=IMPLEMENTER_SYSTEM,
        user=user,
        json_schema_hint=SCHEMA_HINT,
        model=model,
        provider=provider,
        effort=effort or s.cascade_implementer_effort or None,
        temperature=temperature,
        s=s,
    )
    try:
        return _coerce(reply.text)
    except LLMClientError as e:
        # JSON repair pass: ask the same model to fix its own broken output.
        # Saves a full iteration when the only issue is e.g. a missing comma.
        repair_prompt = (
            "Your previous response was not valid JSON or did not match the "
            "required schema. Reply with ONLY the corrected JSON object, no "
            "prose, no markdown fences. Original error:\n"
            f"{str(e)[:600]}\n\n"
            "Original response (broken):\n"
            f"{reply.text[:6000]}"
        )
        repaired = await implementer_chat(
            system="You repair broken JSON outputs from a coding agent. "
                   "Return ONLY a valid JSON object matching the requested schema.",
            user=repair_prompt,
            json_schema_hint=SCHEMA_HINT,
            model=model,
            provider=provider,
            effort=effort or s.cascade_implementer_effort or None,
            temperature=0.0,  # deterministic for repair
            s=s,
        )
        return _coerce(repaired.text)


def _coerce(raw: str) -> ImplementerOutput:
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError:
        try:
            data = parse_json_payload(raw)
        except Exception as e:
            raise LLMClientError(
                f"Implementer output is not valid JSON: {e}\nraw={raw[:500]!r}"
            ) from e
    # Some providers return a bare list of ops.
    if isinstance(data, list):
        data = {"ops": data}
    try:
        return ImplementerOutput.model_validate(data)
    except ValidationError as e:
        raise LLMClientError(f"Implementer output failed schema validation: {e}\nraw={raw[:500]!r}") from e
