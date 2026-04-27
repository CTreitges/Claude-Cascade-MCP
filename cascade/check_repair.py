"""Quality-check self-heal.

Quality checks are shell commands the planner declares to objectively verify
the implementer's output. Real-world planners produce broken checks all the
time — most often a `grep` that scans `.venv/` and finds bare `except:` in
third-party packages. The implementer can never satisfy that, so the cascade
loops forever (one observation: 14 iterations on the same broken check
before the user manually killed the bot).

`repair_quality_check()` is a focused LLM call that takes a single broken
check + its last failure output and asks the model to rewrite ONLY the
shell command (or expected_substring) so the check tests what the planner
*intended*, not what they accidentally wrote. Strictly cheaper than a full
replan: one short prompt → one short JSON reply → drop-in replacement.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .config import Settings, settings
from .llm_client import agent_chat
from .workspace import QualityCheck

log = logging.getLogger("cascade.check_repair")


_REPAIR_SYSTEM_DE = """Du bist ein Quality-Check-Reparateur für einen Coding-Bot.

INPUT: Ein einzelner shell-basierter Quality-Check der seit mehreren Iterationen
in Folge fehlschlägt — sowie die letzte Fehlerausgabe. Der Implementer kann den
Check anscheinend nicht erfüllen.

DEINE AUFGABE: Gib einen reparierten Check zurück. Häufige Muster:
  - grep / find ohne `--exclude-dir=.venv --exclude-dir=__pycache__ --exclude-dir=node_modules`
    → in Drittanbieter-Code werden False Positives gefunden
  - Falscher Pfad-Scope (`./` statt `./scdl_plugin/`)
  - `python` statt `python3` (kein Symlink in vielen Distros)
  - Reguläre Ausdrücke zu breit gefasst
  - `expected_substring` der niemals matched
  - Timeout zu kurz für tatsächliche Laufzeit

BEHALTE die ursprüngliche INTENT bei. Wenn der Check „keine bare excepts im Plugin-Code"
prüfen sollte, soll der reparierte Check genau das tun — nicht das Universum scannen.

ANTWORT: NUR JSON, kein Prose, keine Markdown-Fences:
{
  "command": "<reparierter shell-command>",
  "name": "<optional: leicht angepasster Name>",
  "expected_substring": <optional: string oder null>,
  "timeout_s": <optional: int>,
  "rationale": "<eine Zeile: was war kaputt>"
}

Wenn der Check NICHT reparierbar erscheint (z.B. die Acceptance-Bedingung selbst
ist unrealistisch), gib `command` als leeren String zurück und erkläre kurz im
`rationale`. Der Cascade wird dann hart abbrechen.""".strip()


_REPAIR_SYSTEM_EN = """You repair broken quality-checks for a coding bot.

INPUT: A single shell-based quality check that has failed several iterations in
a row, plus the last failure output. The implementer can't seem to satisfy it.

YOUR JOB: Return a repaired check. Common patterns:
  - grep / find without `--exclude-dir=.venv --exclude-dir=__pycache__ --exclude-dir=node_modules`
    → false positives in third-party code
  - Wrong path scope (`./` instead of `./scdl_plugin/`)
  - `python` vs `python3` (no symlink on many distros)
  - Regex too broad
  - `expected_substring` that will never match
  - Timeout too short for actual runtime

KEEP the original intent. If the check was meant to verify "no bare excepts in
plugin code", the fix should test exactly that — not the universe.

REPLY: JSON ONLY, no prose, no markdown fences:
{
  "command": "<repaired shell command>",
  "name": "<optional: slightly-tweaked name>",
  "expected_substring": <optional: string or null>,
  "timeout_s": <optional: int>,
  "rationale": "<one line: what was broken>"
}

If the check is genuinely unrepairable (e.g. the acceptance criterion itself
is unrealistic), return `command` as empty string and briefly explain in
`rationale`. The cascade will then hard-abort.""".strip()


async def repair_quality_check(
    check: QualityCheck,
    *,
    failure_output: str,
    consecutive_failures: int,
    sub_plan_summary: str | None = None,
    lang: str = "de",
    s: Settings | None = None,
) -> QualityCheck | None:
    """Ask a small LLM to rewrite a broken quality check.

    Returns a new QualityCheck on success, or None if the model declined to
    repair (treated as "give up, hard-abort"). Never raises — on any error
    we return None and the caller falls back to its existing hard-abort
    behaviour.
    """
    s = s or settings()
    system = _REPAIR_SYSTEM_DE if lang == "de" else _REPAIR_SYSTEM_EN
    user = (
        f"CONSECUTIVE FAILURES: {consecutive_failures}\n\n"
        f"BROKEN CHECK:\n"
        f"  name: {check.name!r}\n"
        f"  command: {check.command!r}\n"
        f"  expected_substring: {check.expected_substring!r}\n"
        f"  timeout_s: {check.timeout_s}\n"
        f"\nLAST FAILURE OUTPUT (truncated to 4kB):\n"
        f"{(failure_output or '')[:4000]}\n"
    )
    if sub_plan_summary:
        user += f"\nSUB-TASK CONTEXT:\n{sub_plan_summary[:600]}\n"

    # Use the planner model since it tends to handle structured-JSON well;
    # planner is also already wrapped in with_retry so transient cloud
    # errors won't kill the repair attempt.
    model = s.cascade_planner_model
    try:
        raw = await agent_chat(
            prompt=user,
            model=model,
            system_prompt=system,
            output_json=True,
            timeout_s=120,
            # Tight retry budget — if the repair LLM call itself blows up
            # for >3min, the cascade falls through to hard-abort. Repair
            # is a best-effort optimisation, not a critical path.
            retry_max_total_wait_s=180.0,
            retry_min_backoff_s=10.0,
            retry_max_backoff_s=60.0,
            effort=s.cascade_planner_effort or None,
            s=s,
        )
    except Exception as e:
        log.warning("check_repair: LLM call failed (%s) — caller falls back to hard-abort", e)
        return None

    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError:
        # Try to recover JSON from a code-fenced reply.
        from .claude_cli import parse_json_payload
        try:
            data = parse_json_payload(raw)
        except Exception:
            log.warning(
                "check_repair: response not valid JSON — aborting repair. raw[:300]=%r",
                raw[:300],
            )
            return None

    if not isinstance(data, dict):
        log.warning("check_repair: response not a JSON object — aborting repair")
        return None

    new_command = (data.get("command") or "").strip()
    rationale = (data.get("rationale") or "").strip()[:200]
    if not new_command:
        log.info(
            "check_repair: model declined to repair check %r — rationale=%s",
            check.name, rationale or "(none)",
        )
        return None

    if new_command == check.command:
        # Model returned the same command — no real repair, hard-abort path.
        log.info(
            "check_repair: model returned identical command for %r — no real fix",
            check.name,
        )
        return None

    new_name = (data.get("name") or check.name).strip() or check.name
    expected_substring = data.get("expected_substring")
    if expected_substring is not None and not isinstance(expected_substring, str):
        expected_substring = None
    timeout_s = data.get("timeout_s")
    if not isinstance(timeout_s, int) or timeout_s <= 0:
        timeout_s = check.timeout_s

    repaired = QualityCheck(
        name=new_name,
        command=new_command,
        must_succeed=check.must_succeed,
        expected_substring=expected_substring,
        timeout_s=int(timeout_s),
    )
    log.info(
        "check_repair: %r repaired — old=%r new=%r rationale=%s",
        check.name, check.command, new_command, rationale or "(none)",
    )
    return repaired
