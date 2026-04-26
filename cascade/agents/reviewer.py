"""Reviewer: check Implementer's diff against Plan via `claude -p` (Sonnet)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..claude_cli import parse_json_payload
from ..llm_client import agent_chat
from ..config import Settings, settings
from .planner import Plan


class ReviewResult(BaseModel):
    passed: bool = Field(..., alias="pass")
    feedback: str = ""
    failing_criteria: list[str] = Field(default_factory=list)
    severity: str = "low"  # low | medium | high — used for RLM decision

    model_config = {"populate_by_name": True}


REVIEWER_SYSTEM_EN = """You are the Reviewer in a three-agent loop. Given the
original Plan and the Implementer's git diff, decide whether the change
satisfies every acceptance criterion.

Pass-rules — pass=true ONLY when ALL of these hold:
  1. Every Quality-Check returned ✅ (no ❌, no skipped). If any check
     failed, you MUST set pass=false even if the diff "looks right".
  2. Every entry in `acceptance_criteria` is demonstrably satisfied by
     the diff or the check output. Mention each criterion you checked
     in `feedback` (one short line per criterion).
  3. There is no obvious "TODO", "FIXME", or stub function left in the
     diff that should have been completed by this iteration.
  4. The implementer didn't bypass instructions (e.g. `# noqa` to silence
     a linter the plan asked to keep clean, or `pytest --no-collect`).

If pass=false, `feedback` must explain SPECIFICALLY what the implementer
must do in the next iteration: file + line + concrete change. Generic
"please improve quality" is forbidden.

Severity:
  - "high"   → diff breaks the acceptance criteria materially
  - "medium" → minor issue, but still must be fixed before pass
  - "low"    → cosmetics; should NOT cause a fail unless the plan says so

Reply ONLY with a JSON object — no markdown, no prose.""".strip()


REVIEWER_SYSTEM_DE = """Du bist der Reviewer in einer Drei-Agenten-Schleife.
Anhand des ursprünglichen Plans und des Git-Diffs des Implementers entscheidest
du, ob die Änderung jedes Akzeptanzkriterium erfüllt.

Pass-Regeln — pass=true NUR wenn ALLES davon zutrifft:
  1. Jeder Quality-Check zeigte ✅ (kein ❌, nichts übersprungen). Wenn auch
     nur ein Check fehlschlug, MUSST du pass=false setzen — auch wenn der
     Diff "richtig aussieht".
  2. Jeder Punkt aus `acceptance_criteria` ist durch Diff ODER Check-Output
     nachweislich erfüllt. Nenne in `feedback` jedes geprüfte Kriterium
     (eine knappe Zeile pro Kriterium).
  3. Im Diff steht kein offensichtliches "TODO", "FIXME" oder Stub-Funktion,
     die in dieser Iteration hätte fertiggestellt werden sollen.
  4. Der Implementer hat keine Anweisung umgangen (z.B. `# noqa` um einen
     Linter stumm zu schalten den der Plan sauber halten wollte, oder
     `pytest --no-collect`).

Bei pass=false MUSS `feedback` KONKRET sagen, was im nächsten Durchlauf zu
ändern ist: Datei + Zeile + konkrete Änderung. „Bitte Qualität verbessern"
ist verboten.

Severity:
  - "high"   → Diff verletzt Akzeptanzkriterien materiell
  - "medium" → kleines Problem, muss aber vor pass behoben sein
  - "low"    → Kosmetik; sollte NICHT zum Fail führen außer der Plan
                fordert es ausdrücklich

`feedback` MUSS auf Deutsch sein, klar und umsetzbar — keine Fachfloskeln,
keine Mehrdeutigkeiten. Nenne Datei + Zeilennummer wo möglich.

Antworte AUSSCHLIESSLICH mit einem JSON-Objekt — kein Markdown, keine Prosa.""".strip()


# Backwards-compatible default — used when callers don't pass `lang`.
REVIEWER_SYSTEM = REVIEWER_SYSTEM_EN


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
            + ("\n".join(f"    | {line[:200]}" for line in tail) if tail else "")
        )
    return "\n".join(lines)


def _diff_quality_signals(diff: str) -> list[str]:
    """Cheap heuristics on the raw diff text — surfaced to the reviewer
    as hints so it doesn't miss large-diff / missing-test patterns. The
    reviewer remains the final judge; these just bias attention."""
    signals: list[str] = []
    if not diff:
        return signals
    # Diff size
    plus_lines = sum(
        1 for ln in diff.splitlines()
        if ln.startswith("+") and not ln.startswith("+++")
    )
    if plus_lines >= 500:
        signals.append(
            f"diff is large (+{plus_lines} lines added). If this is a "
            "single sub-task, consider whether the planner should have "
            "decomposed it further."
        )
    # Test-Pflicht: did any non-test file gain a `def ` / `class ` while
    # the test file count is zero?
    new_func_or_class = False
    has_test_change = False
    current_path = ""
    for ln in diff.splitlines():
        if ln.startswith("+++ b/"):
            current_path = ln[6:].strip()
            if "test" in current_path.lower() or current_path.startswith("tests/"):
                has_test_change = True
        elif ln.startswith("+") and not ln.startswith("+++"):
            stripped = ln[1:].lstrip()
            if (
                ("test" not in current_path.lower())
                and not current_path.startswith("tests/")
                and (stripped.startswith("def ") or stripped.startswith("class "))
            ):
                new_func_or_class = True
    if new_func_or_class and not has_test_change:
        signals.append(
            "diff adds new functions/classes in non-test files but does "
            "NOT touch any test file. If the plan didn't explicitly say "
            "'no tests', flag this as missing coverage in `feedback`."
        )
    return signals


def _build_prompt(
    plan: Plan,
    diff: str,
    check_results=None,
    external_context: str | None = None,
    task: str | None = None,
) -> str:
    parts = []
    if task:
        # Original user-facing task — lets the reviewer judge whether the
        # plan even matches the request, not just whether the diff matches
        # the plan. Catches the "plan was wrong from the start" failure.
        parts.append(f"ORIGINAL TASK:\n{task[:1500]}")
    parts.extend([
        f"\nPLAN:\nsummary: {plan.summary}",
        "steps:\n" + "\n".join(f"- {s}" for s in plan.steps),
        "acceptance_criteria:\n" + "\n".join(f"- {a}" for a in plan.acceptance_criteria),
    ])
    if external_context:
        parts.append(f"\n{external_context}")
    if check_results is not None:
        parts.append(f"\nQUALITY CHECK RESULTS:\n{_format_check_results(check_results)}")
        parts.append(
            "Rule: if ANY check failed (❌), you MUST set pass=false and explain "
            "exactly which check needs to pass and how the implementer should fix it."
        )
    parts.append(f"\nDIFF:\n{diff or '(empty diff — implementer produced no changes)'}")
    sig = _diff_quality_signals(diff)
    if sig:
        parts.append("\nDIFF QUALITY SIGNALS (auto-detected):\n" + "\n".join(f"  - {s}" for s in sig))
    if task:
        parts.append(
            "\nIMPORTANT: judge the diff against BOTH the plan AND the original "
            "task. If the plan misses what the user actually asked for, set "
            "pass=false and say so in `feedback` — even if the diff fulfils "
            "every plan step."
        )
    parts.append("\nRespond with a single JSON object matching this schema:\n" + SCHEMA_HINT)
    return "\n".join(parts)


async def call_reviewer(
    plan: Plan,
    diff: str,
    *,
    check_results=None,
    external_context: str | None = None,
    temperature: float | None = None,
    lang: str = "en",
    task: str | None = None,
    s: Settings | None = None,
) -> ReviewResult:
    s = s or settings()
    system = REVIEWER_SYSTEM_DE if lang == "de" else REVIEWER_SYSTEM_EN
    raw = await agent_chat(
        prompt=_build_prompt(plan, diff, check_results, external_context, task=task),
        model=s.cascade_reviewer_model,
        system_prompt=system,
        output_json=True,
        effort=s.cascade_reviewer_effort or None,
        temperature=temperature,
        s=s,
    )
    data = parse_json_payload(raw)
    return ReviewResult.model_validate(data)
