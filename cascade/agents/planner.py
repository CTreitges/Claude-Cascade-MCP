"""Planner: turn a free-form task into a structured plan via `claude -p` (Opus)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..claude_cli import claude_call, parse_json_payload
from ..config import Settings, settings


class RepoHint(BaseModel):
    """Planner's decision about WHERE the implementer should work."""

    kind: Literal["local", "clone", "fresh"] = "fresh"
    path: str | None = None  # absolute or ~-relative local path (for kind=local)
    url: str | None = None   # git clone URL (for kind=clone, or fallback for local)
    rationale: str | None = None


class Plan(BaseModel):
    summary: str = Field(..., description="One-paragraph statement of intent.")
    steps: list[str] = Field(default_factory=list)
    files_to_touch: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    notes: str | None = None
    repo: RepoHint = Field(default_factory=lambda: RepoHint())


PLANNER_SYSTEM = """You are the Planner in a three-agent code-generation loop
(Planner → Implementer → Reviewer). Your only job is to turn the user's task
into a tight, actionable plan that the Implementer can execute. Be concrete:
name files, name functions, name acceptance checks. Do not write code.

You ALSO decide WHERE the implementer should work via the `repo` field:

- repo.kind = "local"  + repo.path = "<absolute path>"
    → use this if the user clearly refers to an existing local project,
      and the path is known (either named in the task or visible in the
      "Locally available git repos" list below).

- repo.kind = "clone"  + repo.url = "<git URL>"
    → use this if the user names a github/gitlab repo by URL/owner-name
      that is NOT in the local list. The runner will clone it on demand.

- repo.kind = "local"  + repo.path = "..."  + repo.url = "..."
    → belt-and-braces: try the local path first, fall back to cloning.

- repo.kind = "fresh"
    → default, used for greenfield tasks ("create a new script that …").

When in doubt prefer "local" with a path from the candidates list — it is
cheaper and matches what the user usually means by "this project" or "the
cascade repo". Only emit "clone" if the URL is unambiguous.""".strip()


SCHEMA_HINT = """{
  "summary": "<one-paragraph intent statement>",
  "steps": ["<step 1>", "<step 2>", ...],
  "files_to_touch": ["relative/path.py", ...],
  "acceptance_criteria": ["concrete check 1", "concrete check 2"],
  "notes": "<optional caveats / open questions, or null>",
  "repo": {
    "kind": "local" | "clone" | "fresh",
    "path": "/absolute/path or null",
    "url":  "https://github.com/... or null",
    "rationale": "<one sentence why>"
  }
}"""


def _build_prompt(task: str, recall_context: str | None, repo_candidates_block: str | None) -> str:
    parts = [f"TASK:\n{task}"]
    if repo_candidates_block:
        parts.append(f"\n{repo_candidates_block}")
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
    repo_candidates_block: str | None = None,
    s: Settings | None = None,
) -> Plan:
    s = s or settings()
    result = await claude_call(
        prompt=_build_prompt(task, recall_context, repo_candidates_block),
        model=s.cascade_planner_model,
        system_prompt=PLANNER_SYSTEM,
        attachments=attachments,
        output_json=True,
    )
    data = parse_json_payload(result.text)
    return Plan.model_validate(data)
