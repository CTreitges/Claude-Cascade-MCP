"""Quick reviewer for direct-actions.

The chat worker can choose a `direct_action` (write_file / edit_env /
place_file / read_file) instead of a full cascade. After the action runs,
this module asks Sonnet (or whichever reviewer model is configured) one
small question: "did the executed action satisfy the user's request?"

Returns ReviewResult-shaped output {passed: bool, feedback: str, severity: str}.
Falls back to a generous "passed=True" if the LLM call fails — direct
actions are scoped tightly enough that an unreachable reviewer should not
block a successful change. The user can always /again or correct manually.
"""

from __future__ import annotations

import logging

from .agents.reviewer import ReviewResult
from .claude_cli import parse_json_payload
from .config import Settings, settings
from .llm_client import LLMClientError, agent_chat

log = logging.getLogger("cascade.quick_review")

_REVIEW_SYSTEM_EN = """You are a tight reviewer for a single direct action that
the chat worker has just taken instead of a full cascade. The original user
request was small and the action set is constrained (write_file / edit_env /
place_file / read_file). Your job is ONLY to check whether the action
matches the user's intent.

Decide:
  - passed=true if the action is a reasonable, complete fulfillment.
  - passed=false ONLY if there is a clear mismatch (wrong path, missing
    value, target file unchanged, security concern). Never reject for
    style / cosmetics.

Reply with JSON ONLY, schema:
  {"passed": bool, "feedback": "<one short sentence>",
   "severity": "low" | "medium" | "high"}
"""


_REVIEW_SYSTEM_DE = """Du bist ein knapper Reviewer für eine einzelne direkte Aktion,
die der Chat-Worker statt einer vollen Cascade ausgeführt hat. Die User-Anfrage
war klein und das Action-Set ist eingegrenzt (write_file / edit_env /
place_file / read_file). Deine Aufgabe ist NUR zu prüfen, ob die Aktion zur
User-Intention passt.

Entscheide:
  - passed=true wenn die Aktion eine vernünftige, vollständige Erfüllung ist.
  - passed=false NUR bei klarer Diskrepanz (falscher Pfad, fehlender Wert,
    Zieldatei unverändert, Sicherheitsbedenken). Niemals wegen Stil /
    Kosmetik ablehnen.

`feedback` muss auf Deutsch sein, ein kurzer Satz.

Antworte AUSSCHLIESSLICH mit JSON, Schema:
  {"passed": bool, "feedback": "<ein kurzer Satz>",
   "severity": "low" | "medium" | "high"}
"""


_REVIEW_SYSTEM = _REVIEW_SYSTEM_EN  # backwards-compatible default


async def review_action(
    *,
    user_request: str,
    action_kind: str,
    action_summary: str,
    action_log: list[str],
    files_touched: list[str],
    output: str | None = None,
    lang: str = "en",
    s: Settings | None = None,
) -> ReviewResult:
    s = s or settings()
    system = _REVIEW_SYSTEM_DE if lang == "de" else _REVIEW_SYSTEM_EN
    prompt_parts = [
        f"USER REQUEST:\n{user_request}",
        f"\nACTION TAKEN:\n  kind: {action_kind}\n  summary: {action_summary}",
    ]
    if action_log:
        prompt_parts.append("  log:\n    " + "\n    ".join(action_log))
    if files_touched:
        prompt_parts.append("  files_touched: " + ", ".join(files_touched))
    if output:
        prompt_parts.append(f"\nACTION OUTPUT (may be partial):\n{output[:4000]}")
    prompt = "\n".join(prompt_parts)
    try:
        raw = await agent_chat(
            prompt=prompt,
            model=s.cascade_reviewer_model,
            system_prompt=system,
            output_json=True,
            effort=s.cascade_reviewer_effort or None,
            timeout_s=60,
            s=s,
        )
        data = parse_json_payload(raw)
        return ReviewResult.model_validate(data)
    except (LLMClientError, Exception) as e:
        log.warning("quick_review failed (%s) — defaulting to passed=true", e)
        fb = (
            f"Reviewer nicht erreichbar ({type(e).__name__}); standardmäßig akzeptiert."
            if lang == "de"
            else f"reviewer unreachable ({type(e).__name__}); accepted by default."
        )
        return ReviewResult(passed=True, feedback=fb, severity="low")
