"""Classifies an inbound bot message: real coding task vs. casual chat.

Implementation: a single fast `claude -p` call (configurable via
CASCADE_TRIAGE_MODEL, default Sonnet 4.6) returning JSON
{is_task: bool, task: str|None, reply: str|None, direct_action: ...}.

Falls back to a regex heuristic if claude is unreachable so the bot stays
responsive even if claude credentials are missing.

Path-prevalidation: when the LLM proposes a direct_action with a target
path, this module verifies that path against `simple_actions._ALLOWED_ROOTS`
*before* returning. Otherwise the action would be accepted by the bot and
only fail at execution time, leaving the user with an opaque error.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from .claude_cli import parse_json_payload
from .llm_client import LLMClientError, agent_chat
from .config import Settings, settings

log = logging.getLogger("cascade.triage")


@dataclass
class TriageResult:
    is_task: bool
    task: str | None      # the (possibly refined) task to dispatch
    reply: str | None     # if is_task=False, the friendly reply to send back
    via: str              # "claude" | "heuristic" | "disabled"
    # Optional 3rd mode: a small direct-action that skips the full cascade
    # but still gets a quick reviewer pass. Used for setup-y requests like
    # "set FOO in .env to bar" or "write this content to ~/.config/scdl/x.yaml".
    direct_action: dict | None = None


SYSTEM_DE = """Du bist der Dispatcher und Konversations-Layer eines Coding-Bots namens Cascade-Bot.
Klassifiziere die Nachricht des Users und antworte AUSSCHLIESSLICH mit JSON in EINEM von drei Modi:

MODUS 1 — *Direkte Aktion* (für KLEINE, klar umrissene Setup-/Config-Tasks):
  → {"is_task": true, "task": "<einsatzfertiger Auftrag>",
     "direct_action": {"kind": "<...>", "summary": "<one sentence>",
                       "params": {...}}}

  Erlaubte Action-Kinds:
    - "write_file":  params={target: "<absolute/~-path>", content: "<text>", mode: 0o644}
    - "edit_env":    params={target: ".../.env", key: "FOO", value: "bar"}
    - "place_file":  params={source: "<existing path>", target: "<.../neuer-pfad>", mode: 0o600}
    - "read_file":   params={target: "<path>"}

  Nutze direct_action NUR wenn:
    * Die Anfrage ein einziges Datei-Setup ist (chmod, .env-Update, JSON-Drop)
    * Der Pfad innerhalb erlaubter Roots liegt (~/.config, ~/projekte,
      ~/claude-cascade, /tmp).
    * Du den genauen Wert / Inhalt aus der Konversation/User-Facts ableiten
      kannst — niemals raten.
  Setze trotzdem `is_task: true` damit der Reviewer nachprüft.

MODUS 2 — *Volle Cascade* (für richtige Code-Tasks):
  → {"is_task": true, "task": "<die Aufgabe in 1-2 Sätzen>"}

  Nutze diesen Modus für: Feature-Implementierung, Bug-Fix, Multi-File-Änderung,
  Plugin-Entwicklung, Test-Generierung, Refactoring. Alles wo geplant + iteriert
  werden muss.

MODUS 3 — *Konversation*:
  → {"is_task": false, "reply": "<freundliche, knappe Antwort auf Deutsch>"}

  Begrüssung, Smalltalk, Frage zu dir/Status, Frage zu vorherigen Tasks,
  generelle Diskussion, Klärungsfrage, Dank, etc.

Wenn der User eine Frage zu einem vorherigen Task stellt ("was hast du gemacht?",
"wo liegt das?", "zeig mir den code", etc.) und du Kontext zu vorherigen Tasks
hast — beziehe dich konkret darauf in deiner Antwort (Task-ID, Workspace-Pfad,
geänderte Dateien, Plan-Zusammenfassung).

DATEI-AWARENESS — sehr wichtig:
Wenn der User nach einer Datei fragt ("hast du die json?", "wo ist die datei?",
"erinnerst du dich an die credentials?"), schau in den unten angehängten Block
"KÜRZLICH HOCHGELADENE DATEIEN" und in den "CHAT-VERLAUF" mit `[FILE: ...]`-
Markern. Antworte konkret mit Dateiname + abgelegtem Pfad + Inhalts-Stichpunkten.
NIEMALS antworten "ich habe keine Datei erhalten" wenn der Block existiert.

WICHTIG zur Erinnerung: Schau VOR dem Antworten in alle Kontextquellen unten,
falls vorhanden:
- NUTZER-FAKTEN (persistent gespeicherte Pfade, Projekt-IDs, Credentials)
- KÜRZLICH HOCHGELADENE DATEIEN (24h-Fenster, mit Pfad + Klassifikation)
- CHAT-VERLAUF (jüngste Nachrichten, inkl. Datei-Inhalt-Snippets)
- FRÜHERER CHAT-VERLAUF (Zusammenfassungen älterer Sessions)
- SUCH-TREFFER (bei direkter Bezugnahme via Volltextsuche)
Wenn keine Quelle die Frage beantwortet, sag das ehrlich — aber prüfe ALLE.

Antworte NUR mit dem JSON-Objekt, keine Markdown-Fences, kein Prosa drumherum."""

SYSTEM_EN = """You are the dispatcher and conversation layer of Cascade-Bot.
Classify the user's message and reply WITH JSON ONLY in one of three modes:

MODE 1 — *Direct action* (for small, well-defined setup / config tasks):
  → {"is_task": true, "task": "<imperative summary>",
     "direct_action": {"kind": "<...>", "summary": "<one sentence>",
                       "params": {...}}}

  Allowed action kinds:
    - "write_file":  params={target, content, mode}
    - "edit_env":    params={target, key, value}
    - "place_file":  params={source, target, mode}
    - "read_file":   params={target}

  Use direct_action ONLY when:
    * The request is a single config/file setup (chmod, .env update, JSON drop)
    * The target lives inside allowed roots (~/.config, ~/projekte,
      ~/claude-cascade, /tmp)
    * You can derive the exact value/content from the conversation /
      user_facts — never guess.
  is_task remains true so the reviewer still checks the result.

MODE 2 — *Full cascade* (for real code work):
  → {"is_task": true, "task": "<the task in 1-2 sentences>"}

  Use for: feature implementation, bug fix, multi-file change, plugin
  development, test generation, refactoring — anything that needs planning
  + iteration.

MODE 3 — *Conversation*:
  → {"is_task": false, "reply": "<friendly short reply>"}

If the user asks about a previous task ("what did you do?", "where is it?",
"show me the code"), and you have context about previous tasks — refer to it
concretely (task id, workspace path, changed files, plan summary).

FILE-AWARENESS — very important:
If the user asks about a file ("do you have the json?", "where is the file?",
"remember the credentials?"), look at the "RECENT UPLOADS" block below and at
the "CONVERSATION" block with `[FILE: ...]` markers. Answer concretely with
file name + staged path + content highlights.
NEVER reply "I haven't received a file" when those blocks exist.

IMPORTANT for memory: BEFORE answering, check ALL context sources below,
if present:
- USER FACTS (persistently stored paths, project IDs, credential refs)
- RECENT UPLOADS (24h window, with path + classification)
- CONVERSATION (latest messages incl. file-content snippets)
- EARLIER CONVERSATIONS (summaries of older sessions)
- SEARCH HITS (FTS hits when the user references something specific)
If none of these answer the question, say so honestly — but check every block.

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


def _validate_direct_action(da: dict, *, lang: str = "de") -> dict | None:
    """Return `da` unchanged if it's a known kind AND its target path lives
    under the allowlist. Returns None (and logs) otherwise — the caller
    then falls back to a cascade dispatch.

    Pre-validation prevents the "looks fine, fails at runtime" pattern where
    the user sees an opaque "outside allowed roots" error after triage has
    already announced the action.
    """
    from .simple_actions import is_known_kind, is_target_in_allowlist

    kind = da.get("kind")
    if not isinstance(kind, str) or not is_known_kind(kind):
        log.info(
            "triage: rejecting direct_action — unknown kind=%r",
            kind,
        )
        return None
    params = da.get("params") or {}
    target = params.get("target")
    if not target or not isinstance(target, str):
        log.info(
            "triage: rejecting direct_action %s — missing target", kind,
        )
        return None
    if not is_target_in_allowlist(target):
        log.info(
            "triage: rejecting direct_action %s — target %r outside allowlist",
            kind, target,
        )
        return None
    # `place_file` additionally needs a `source` that exists; we let the
    # runtime double-check that one (it's a transient state that may flip
    # between triage and execution).
    return da


async def triage(
    message: str,
    *,
    lang: str = "de",
    s: Settings | None = None,
    context: str | None = None,
    history: list[dict] | None = None,
    memory_block: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
) -> TriageResult:
    """Classify one user message.

    Context channels (any of which may be None):
      - `memory_block`: pre-built structured block from `ChatMemory.build_context()`
        (USER FACTS / RECENT UPLOADS / CONVERSATION / EARLIER / SEARCH HITS).
        Preferred over the legacy `context` + `history` split — when given,
        those two are skipped because `memory_block` already covers them.
      - `context`: legacy free-form context block (e.g. recent task summaries).
      - `history`: legacy list of `{role, text, ts}` dicts.

    Returns a `TriageResult`. `via` reflects which path produced the answer
    ("claude" / "ollama" / "heuristic" / "disabled").
    """
    s = s or settings()
    if not s.cascade_triage_enabled:
        return TriageResult(is_task=True, task=message, reply=None, via="disabled")

    triage_model = model or s.cascade_triage_model
    started = time.monotonic()
    log.debug(
        "triage: start (model=%s, lang=%s, msg_len=%d, memory_block=%s, "
        "context=%s, history=%d)",
        triage_model, lang, len(message),
        len(memory_block) if memory_block else 0,
        len(context) if context else 0,
        len(history) if history else 0,
    )

    system = SYSTEM_DE if lang == "de" else SYSTEM_EN
    if memory_block:
        # `memory_block` is already structured + headed (built by ChatMemory).
        # Don't wrap it again — just append.
        system = f"{system}\n\n{memory_block}"
    else:
        if context:
            header = "=== Vorheriger Kontext ===" if lang == "de" else "=== Previous context ==="
            system = f"{system}\n\n{header}\n{context}"
        if history:
            h_header = "=== Bisheriger Chat-Verlauf ===" if lang == "de" else "=== Conversation so far ==="
            lines = []
            for m in history:
                tag = ("User" if m.get("role") == "user" else "Bot")
                txt = (m.get("text") or "").strip().replace("\n", " ")
                if len(txt) > 1500:
                    txt = txt[:1500] + "…"
                lines.append(f"{tag}: {txt}")
            system = f"{system}\n\n{h_header}\n" + "\n".join(lines)
    user_msg = ("NACHRICHT:" if lang == "de" else "MESSAGE:") + f"\n{message}"
    try:
        raw = await agent_chat(
            prompt=user_msg,
            model=model or s.cascade_triage_model,
            system_prompt=system,
            output_json=True,
            # Timeout per individual `claude -p` attempt. `with_retry`
            # (inside agent_chat) keeps trying on rate-limit / timeout signals
            # but with a TIGHT budget: triage is on the user-facing hot path,
            # we'd rather flip to heuristic after ~3 min than block the chat
            # for hours.
            timeout_s=90,
            retry_max_total_wait_s=180.0,
            retry_min_backoff_s=10.0,
            retry_max_backoff_s=60.0,
            effort=s.cascade_triage_effort or None,
            temperature=temperature,
            s=s,
        )
    except LLMClientError as e:
        log.warning("triage llm call failed (%s) — falling back to heuristic", e)
        from .error_log import log_error
        await log_error("triage.llm_call", e, model=model or s.cascade_triage_model, lang=lang)
        return _heuristic(message, lang)

    try:
        data = parse_json_payload(raw)
    except Exception as e:
        log.warning("triage JSON parse failed (%s) — falling back to heuristic", e)
        from .error_log import log_error
        await log_error(
            "triage.parse_json",
            e,
            model=model or s.cascade_triage_model,
            raw_preview=raw[:300] if isinstance(raw, str) else str(raw)[:300],
        )
        return _heuristic(message, lang)

    via = "claude" if triage_model.startswith("claude-") else "ollama"
    is_task = bool(data.get("is_task"))
    direct_action = data.get("direct_action")
    if isinstance(direct_action, dict) and direct_action.get("kind"):
        direct_action = _validate_direct_action(direct_action, lang=lang)
    else:
        direct_action = None

    latency_ms = int((time.monotonic() - started) * 1000)
    mode = (
        "direct_action" if direct_action
        else ("cascade" if is_task else "chat")
    )
    log.info(
        "triage: done mode=%s via=%s model=%s latency_ms=%d",
        mode, via, triage_model, latency_ms,
    )

    if is_task:
        return TriageResult(
            is_task=True,
            task=str(data.get("task") or message),
            reply=None,
            via=via,
            direct_action=direct_action,
        )
    return TriageResult(
        is_task=False,
        task=None,
        reply=str(data.get("reply") or ""),
        via=via,
    )
