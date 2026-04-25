"""Classifies an inbound bot message: real coding task vs. casual chat.

Implementation: a single fast `claude -p` call (configurable via
CASCADE_TRIAGE_MODEL, default Sonnet 4.6) returning JSON
{is_task: bool, task: str|None, reply: str|None}.

Falls back to a regex heuristic if claude is unreachable so the bot stays
responsive even if claude credentials are missing.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from .claude_cli import ClaudeCliError, claude_call, parse_json_payload
from .config import Settings, settings

log = logging.getLogger("cascade.triage")


@dataclass
class TriageResult:
    is_task: bool
    task: str | None      # the (possibly refined) task to dispatch
    reply: str | None     # if is_task=False, the friendly reply to send back
    via: str              # "claude" | "heuristic" | "disabled"


SYSTEM_DE = """Du bist der Dispatcher und Konversations-Layer eines Coding-Bots namens Claude-Cascade.
Klassifiziere die Nachricht des Users und antworte AUSSCHLIESSLICH mit JSON.

- Wenn es eine konkrete Coding-/Implementierungs-Aufgabe ist
  (Datei erstellen/ändern, Bug fixen, Feature bauen, Skript schreiben):
  → {"is_task": true, "task": "<die Aufgabe in 1-2 Sätzen, präzisiert wenn nötig>"}

- Sonst (Begrüssung, Smalltalk, Frage zu dir/Status, Frage zu vorherigen Tasks,
  generelle Diskussion, Klärungsfrage, Dank, etc.):
  → {"is_task": false, "reply": "<freundliche, knappe Antwort auf Deutsch>"}

Wenn der User eine Frage zu einem vorherigen Task stellt ("was hast du gemacht?",
"wo liegt das?", "zeig mir den code", etc.) und du Kontext zu vorherigen Tasks
hast — beziehe dich konkret darauf in deiner Antwort (Task-ID, Workspace-Pfad,
geänderte Dateien, Plan-Zusammenfassung).

Antworte NUR mit dem JSON-Objekt, keine Markdown-Fences, kein Prosa drumherum."""

SYSTEM_EN = """You are the dispatcher and conversation layer of a coding bot called Claude-Cascade.
Classify the user's message and reply WITH JSON ONLY.

- If it is a concrete coding/implementation task
  (create/modify a file, fix a bug, build a feature, write a script):
  → {"is_task": true, "task": "<the task in 1-2 sentences, refined if needed>"}

- Otherwise (greeting, smalltalk, question about you/status, question about
  previous tasks, general discussion, clarification, thanks, …):
  → {"is_task": false, "reply": "<friendly short reply in English>"}

If the user asks about a previous task ("what did you do?", "where is it?",
"show me the code"), and you have context about previous tasks — refer to it
concretely (task id, workspace path, changed files, plan summary).

Output ONLY the JSON object, no markdown fences, no prose around it."""


# Imperative German verbs that almost always mean "do this coding task".
_IMPERATIVE_HEURISTIC = re.compile(
    r"\b("
    r"erstelle|mach|baue|implementier|programmiere|schreib|"
    r"füg(e)?\s+\w+\s+(hinzu|zu)|fix|repar(ier)?|debug|refaktor|"
    r"create|build|implement|write|add|fix|refactor"
    r")\b",
    re.IGNORECASE,
)


def _heuristic(message: str, lang: str) -> TriageResult:
    if _IMPERATIVE_HEURISTIC.search(message):
        return TriageResult(is_task=True, task=message, reply=None, via="heuristic")
    fallback = (
        "Ok, ich bin da. Sag mir bitte konkret, was ich bauen soll."
        if lang == "de"
        else "Got it — let me know what you'd like me to build."
    )
    return TriageResult(is_task=False, task=None, reply=fallback, via="heuristic")


async def triage(
    message: str,
    *,
    lang: str = "de",
    s: Settings | None = None,
    context: str | None = None,
) -> TriageResult:
    s = s or settings()
    if not s.cascade_triage_enabled:
        return TriageResult(is_task=True, task=message, reply=None, via="disabled")

    system = SYSTEM_DE if lang == "de" else SYSTEM_EN
    if context:
        header = "=== Vorheriger Kontext ===" if lang == "de" else "=== Previous context ==="
        system = f"{system}\n\n{header}\n{context}"
    user_msg = ("NACHRICHT:" if lang == "de" else "MESSAGE:") + f"\n{message}"
    try:
        result = await claude_call(
            prompt=user_msg,
            model=s.cascade_triage_model,
            system_prompt=system,
            output_json=True,
            timeout_s=60,
        )
    except ClaudeCliError as e:
        log.warning("triage claude call failed (%s) — falling back to heuristic", e)
        return _heuristic(message, lang)

    try:
        data = parse_json_payload(result.text)
    except Exception as e:
        log.warning("triage JSON parse failed (%s) — falling back to heuristic", e)
        return _heuristic(message, lang)

    is_task = bool(data.get("is_task"))
    if is_task:
        return TriageResult(
            is_task=True,
            task=str(data.get("task") or message),
            reply=None,
            via="claude",
        )
    return TriageResult(
        is_task=False,
        task=None,
        reply=str(data.get("reply") or ""),
        via="claude",
    )
