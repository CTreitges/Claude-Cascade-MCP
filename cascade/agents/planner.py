"""Planner: turn a free-form task into a structured plan via `claude -p` (Opus)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..claude_cli import parse_json_payload
from ..llm_client import agent_chat
from ..config import Settings, settings
from ..workspace import QualityCheck  # re-exported


class RepoHint(BaseModel):
    """Planner's decision about WHERE the implementer should work."""

    kind: Literal["local", "clone", "fresh"] = "fresh"
    path: str | None = None  # absolute or ~-relative local path (for kind=local)
    url: str | None = None   # git clone URL (for kind=clone, or fallback for local)
    rationale: str | None = None


class SubTask(BaseModel):
    """One independently-runnable slice of a larger task. Each sub-task gets
    its own implement→review loop on the SHARED workspace; later sub-tasks
    see the files written by earlier ones."""

    name: str = Field(..., description="Short label, e.g. 'plugin-skeleton'.")
    summary: str
    steps: list[str] = Field(default_factory=list)
    files_to_touch: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    quality_checks: list[QualityCheck] = Field(default_factory=list)
    depends_on: list[str] = Field(
        default_factory=list,
        description="Names of sub-tasks that must finish before this one starts.",
    )


class Plan(BaseModel):
    summary: str = Field(..., description="One-paragraph statement of intent.")
    steps: list[str] = Field(default_factory=list)
    files_to_touch: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    notes: str | None = None
    repo: RepoHint = Field(default_factory=lambda: RepoHint())
    quality_checks: list[QualityCheck] = Field(default_factory=list)
    # Optional decomposition. If non-empty, the supervisor runs each sub-task
    # as its own mini-cascade on the shared workspace, then a final
    # integration review across the cumulative diff.
    subtasks: list[SubTask] = Field(default_factory=list)
    decompose_rationale: str | None = None
    # Trivial-task shortcut. Set ONLY when the task is small enough that
    # full plan-implement-review iteration would be wasteful. The cascade
    # applies these ops directly and runs ONE reviewer pass — ~30s instead
    # of 2-3min. Implementer/iter-loop is skipped.
    direct_ops: list = Field(
        default_factory=list,
        description="Ready-to-apply file ops for trivial tasks. "
        "Each entry is {op:'write'|'edit'|'delete', path:'rel/path', "
        "content?:'...', find?:'...', replace?:'...'} matching FileOp.",
    )
    direct_rationale: str | None = None


PLANNER_SYSTEM_EN = """You are the Planner in a three-agent code-generation loop
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
cascade repo". Only emit "clone" if the URL is unambiguous.

PLATFORM NOTES — the runner is Linux. Always use `python3` (NOT `python`) and
`python3 -m pytest` (NOT bare `pytest`) in any quality_checks command —
the `python` symlink is not guaranteed to exist. Use POSIX shell builtins
(`test -f`, `wc -l`, `grep -q`, `cat`) for cheap file checks.

TRIVIAL-TASK SHORTCUT — when the task is small enough that the full
plan-implement-review-iter loop would waste time/tokens, set
`direct_ops: [...]` instead of `steps`/`files_to_touch`/`acceptance_criteria`.
The cascade then applies your ops directly and only runs ONE reviewer pass.

Use direct_ops when ALL of these hold:
  - 1 to 3 file operations total (write/edit/delete)
  - You can write the COMPLETE final content right now without ambiguity
  - No exploration needed (you're not "let's see what foo.py contains")
  - No multi-step dependency (file B depending on file A's output)

Examples that ARE trivial (use direct_ops):
  - "Schreibe hello.py das hi druckt" → write hello.py with print('hi')
  - "Ergänze TODO in README.md" → edit README.md, find/replace
  - "Lege eine .gitignore mit Python-Defaults an" → write .gitignore

Examples that are NOT trivial (regular plan):
  - "Bau ein FastAPI-Endpoint mit Pydantic-Validation" → multi-file, plan
  - "Fix den Bug in pipeline.py wo …" → must read file first, plan
  - Anything with `subtasks`

Set `direct_rationale` to a short sentence explaining why this is trivial.
Leave `steps`, `files_to_touch`, `acceptance_criteria`, `quality_checks`
EMPTY when emitting direct_ops — they would just confuse the cascade.

DECOMPOSITION — for big or multi-component tasks (typically: ≥4 distinct
files, ≥3 unrelated concerns like 'CLI + tests + Docker + README', or a
clear sequence like 'first scaffold, then implement, then test'), split the
work into 2-6 sub-tasks via the `subtasks` field. Each sub-task is a
self-contained slice with its own steps/files/acceptance/quality_checks.
The supervisor runs them sequentially on the SAME workspace, so later
sub-tasks see the files written by earlier ones. Use `depends_on` to mark
ordering when needed (default: linear order matches array order).

When you decompose, the top-level `steps`/`files_to_touch`/`quality_checks`
should describe the OVERALL goal — but the actual implementer-iterations
will use the sub-task fields, not the top-level ones. Set
`decompose_rationale` to one sentence explaining why decomposition helps
("multi-component plugin with independent layers", etc.).

When NOT to decompose: small focused tasks (single file, single concern,
single test). For those, leave `subtasks: []` and just fill the top-level
fields as before — the loop runs as a single mini-cascade.

QUALITY CHECKS — define `quality_checks` as objective, scriptable verifications
that the runner will execute in the workspace after every iteration. Each
check has a name, a shell command (cwd = workspace root), an optional expected
substring, and a timeout (seconds). The loop only succeeds when ALL checks
pass AND the reviewer signs off.

Examples:
  - {"name":"pytest","command":"python -m pytest tests/test_x.py -q","timeout_s":60}
  - {"name":"syntax","command":"python -c 'import foo'","timeout_s":15}
  - {"name":"file exists","command":"test -f hello.py","timeout_s":5}
  - {"name":"line count","command":"wc -l < ANALYSE.md","expected_substring":"5","timeout_s":5}

Choose a SMALL set of cheap, fast, deterministic checks. Don't run the full
suite if a focused command suffices. If the task is purely descriptive
(write a markdown file), use file-existence + content checks. Avoid checks
that depend on network. If no objective check is meaningful (rare), return
an empty list — but think first whether `wc`, `grep`, `test -f`, `python -c
'import …'`, `python -m py_compile` could verify the work.""".strip()


# German-language Planner prompt. Schema field names stay English (the
# downstream Plan Pydantic model expects `summary`, `steps`, `subtasks`,
# `direct_ops`, etc.) — only the surrounding instructions are translated.
PLANNER_SYSTEM_DE = """Du bist der Planner in einer Drei-Agenten-Code-Generierungs-
Schleife (Planner → Implementer → Reviewer). Deine einzige Aufgabe ist es, die
User-Aufgabe in einen knappen, umsetzbaren Plan zu übersetzen, den der Implementer
direkt ausführen kann. Sei konkret: Nenne Dateinamen, Funktionsnamen, prüfbare
Akzeptanzkriterien. Schreibe selbst KEINEN Code.

Du entscheidest auch WO der Implementer arbeiten soll, über das `repo`-Feld:

- repo.kind = "local"  + repo.path = "<absoluter Pfad>"
    → Wenn der User klar auf ein bestehendes lokales Projekt verweist und
      der Pfad bekannt ist (im Task genannt oder in der unten angehängten
      "Locally available git repos"-Liste sichtbar).

- repo.kind = "clone"  + repo.url = "<git URL>"
    → Wenn der User ein github/gitlab-Repo per URL/owner-name nennt, das
      NICHT in der lokalen Liste ist. Der Runner cloned bei Bedarf.

- repo.kind = "local" + repo.path + repo.url
    → Sicherheitsgurt: lokal versuchen, sonst clonen.

- repo.kind = "fresh"
    → Default für Greenfield-Tasks ("erstelle ein neues Skript, das …").

Im Zweifel "local" mit einem Pfad aus der Kandidaten-Liste — günstiger und
trifft meist die User-Intention von "diesem Projekt" / "dem cascade-repo".
"clone" nur bei eindeutiger URL.

PLATTFORM-HINWEISE — der Runner ist Linux. Verwende immer `python3` (NICHT
`python`) und `python3 -m pytest` (NICHT bare `pytest`) in jedem
quality_checks-Befehl — der `python`-Symlink ist nicht garantiert vorhanden.
Nutze POSIX-Shell-Builtins (`test -f`, `wc -l`, `grep -q`, `cat`) für günstige
Datei-Checks.

TRIVIAL-TASK-SHORTCUT — wenn die Aufgabe klein genug ist, dass der volle
plan-implement-review-iter-Loop Zeit/Tokens verschwenden würde, setze stattdessen
`direct_ops: [...]` statt `steps`/`files_to_touch`/`acceptance_criteria`.
Die Cascade wendet deine Ops dann direkt an und führt nur EINE Reviewer-Pass durch.

Nutze direct_ops wenn ALLE diese Bedingungen erfüllt sind:
  - 1 bis 3 Datei-Operationen total (write/edit/delete)
  - Du kannst den FERTIGEN Inhalt jetzt ohne Mehrdeutigkeit hinschreiben
  - Keine Exploration nötig (kein "lass mich schauen, was foo.py enthält")
  - Keine Multi-Step-Abhängigkeit (Datei B hängt von A's Ausgabe ab)

Beispiele die TRIVIAL sind (use direct_ops):
  - "Schreibe hello.py das hi druckt" → write hello.py mit print('hi')
  - "Ergänze TODO in README.md" → edit README.md, find/replace
  - "Lege eine .gitignore mit Python-Defaults an" → write .gitignore

Beispiele die NICHT trivial sind (regular plan):
  - "Bau ein FastAPI-Endpoint mit Pydantic-Validation" → multi-file, plan
  - "Fix den Bug in pipeline.py wo …" → muss Datei zuerst lesen, plan
  - Alles mit `subtasks`

Setze `direct_rationale` auf einen kurzen Satz, warum das trivial ist.
Lasse `steps`, `files_to_touch`, `acceptance_criteria`, `quality_checks`
LEER bei direct_ops — sie würden die Cascade nur verwirren.

DECOMPOSITION — bei großen oder mehrteiligen Tasks (typischerweise: ≥4 verschiedene
Dateien, ≥3 unverbundene Themen wie 'CLI + Tests + Docker + README', oder eine
klare Sequenz wie 'erst Skelett, dann Implementierung, dann Tests'), teile die
Arbeit in 2-6 Sub-Tasks via `subtasks`. Jeder Sub-Task ist ein in sich
geschlossener Schnitt mit eigenen steps/files/acceptance/quality_checks.
Der Supervisor führt sie sequentiell auf demselben Workspace aus, spätere
Sub-Tasks sehen die Dateien der früheren. Nutze `depends_on` für explizite
Reihenfolge (Default: lineare Reihenfolge der Liste).

Bei Decomposition sollten die Top-Level-`steps`/`files_to_touch`/`quality_checks`
das GESAMT-Ziel beschreiben — die tatsächlichen Implementer-Iterationen nutzen
aber die Sub-Task-Felder, nicht die Top-Level. Setze `decompose_rationale`
auf einen Satz warum Decomposition hilft.

Wenn NICHT decomposed werden soll: kleine fokussierte Tasks (eine Datei, ein
Thema, ein Test). Dafür `subtasks: []` lassen und nur Top-Level füllen.

QUALITY CHECKS — definiere `quality_checks` als objektive, scriptbare
Verifikationen, die der Runner nach jeder Iteration im Workspace ausführt.
Jeder Check hat name, shell-command (cwd = workspace root), optionale
expected_substring, und timeout (Sekunden). Der Loop ist nur erfolgreich wenn
ALLE Checks bestehen UND der Reviewer freigibt.

Beispiele:
  - {"name":"pytest","command":"python3 -m pytest tests/test_x.py -q","timeout_s":60}
  - {"name":"syntax","command":"python3 -c 'import foo'","timeout_s":15}
  - {"name":"file exists","command":"test -f hello.py","timeout_s":5}
  - {"name":"line count","command":"wc -l < ANALYSE.md","expected_substring":"5","timeout_s":5}

Wähle eine KLEINE Menge günstiger, schneller, deterministischer Checks. Lass
nicht die ganze Suite laufen wenn ein fokussierter Befehl reicht. Bei rein
beschreibenden Tasks (Markdown-Datei schreiben) Datei-Existenz + Inhalts-Checks.
Vermeide Checks die Netzwerk brauchen. Wenn kein objektiver Check sinnvoll ist
(selten), gib leere Liste zurück — aber überlege erst ob `wc`, `grep`,
`test -f`, `python3 -c 'import …'`, `python3 -m py_compile` die Arbeit
verifizieren könnten.

Antworte AUSSCHLIESSLICH mit einem JSON-Objekt nach dem Schema — keine
Markdown-Fences, keine Prosa.""".strip()


# Backwards-compatible default
PLANNER_SYSTEM = PLANNER_SYSTEM_EN


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
  },
  "quality_checks": [
    {"name": "...", "command": "...", "must_succeed": true,
     "expected_substring": null, "timeout_s": 60},
    ...
  ],
  "subtasks": [
    {
      "name": "<short-id>",
      "summary": "<what this slice produces>",
      "steps": ["...", "..."],
      "files_to_touch": ["..."],
      "acceptance_criteria": ["..."],
      "quality_checks": [{"name":"...","command":"...","timeout_s":30}],
      "depends_on": []
    }
  ],
  "decompose_rationale": "<why splitting helps, or null if subtasks is []>",
  "direct_ops": [
    {"op": "write",  "path": "relative/path.py", "content": "<full file body>"},
    {"op": "edit",   "path": "relative/path.py", "find": "<unique snippet>",
     "replace": "<new snippet>"},
    {"op": "delete", "path": "relative/path.py"}
  ],
  "direct_rationale": "<one sentence why this task is trivial, or null if not applicable>"
}"""


def _build_prompt(
    task: str,
    recall_context: str | None,
    repo_candidates_block: str | None,
    replan_feedback: str | None = None,
    external_context: str | None = None,
) -> str:
    parts = [f"TASK:\n{task}"]
    if external_context:
        parts.append(f"\n{external_context}")
    if repo_candidates_block:
        parts.append(f"\n{repo_candidates_block}")
    if recall_context:
        parts.append(f"\nRELEVANT MEMORIES:\n{recall_context}")
    if replan_feedback:
        parts.append(
            "\nRE-PLAN — the previous plan failed in the implement-review loop. "
            "Use the history below to fix the plan and especially the "
            "`quality_checks` (often the failure is a wrong command). "
            "Be concrete about what to change.\n"
            f"\n{replan_feedback}"
        )
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
    replan_feedback: str | None = None,
    external_context: str | None = None,
    temperature: float | None = None,
    lang: str = "en",
    s: Settings | None = None,
) -> Plan:
    s = s or settings()
    system = PLANNER_SYSTEM_DE if lang == "de" else PLANNER_SYSTEM_EN
    raw = await agent_chat(
        prompt=_build_prompt(
            task, recall_context, repo_candidates_block, replan_feedback, external_context,
        ),
        model=s.cascade_planner_model,
        system_prompt=system,
        attachments=attachments,
        output_json=True,
        effort=s.cascade_planner_effort or None,
        temperature=temperature,
        s=s,
    )
    data = parse_json_payload(raw)
    return Plan.model_validate(data)
