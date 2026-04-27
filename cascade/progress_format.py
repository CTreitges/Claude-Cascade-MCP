"""Render a cascade progress event as one or more milestone-display lines.

The Telegram bot's runner has its own event formatter using Telegram
Markdown — emojis + `_italic_` + `*bold*` + `\\.` escapes etc. The
slash-command in Claude Code wants the SAME content but rendered as
plain text (no Markdown escapes, since Claude Code shows the lines
literally).

This module is the shared source of truth for that formatting. The
Telegram bot continues to use its own markdown variant (different
visual surface). The MCP tool `cascade_progress` and the
`/cascade` slash-command both consume `format_milestone()` so the
two interfaces stay in sync as we add new event kinds.

Key design rules:

  - Plain text out — no `*bold*`, no escape sequences. Emojis are fine.
  - One "milestone" event may render to 1-3 lines (e.g. `planned`
    optionally lists sub-task names).
  - Returns `[]` for events we deliberately don't surface
    (`implementing`, `reviewing`, `started` — too noisy on their own).
  - Never raises. Bad payloads silently produce a one-line fallback.
"""

from __future__ import annotations

import json


# Events that ARE worth showing live (the rest are too noisy or
# already covered by their counterpart events — e.g. we show
# "implemented" but skip "implementing").
_MILESTONE_EVENTS: frozenset[str] = frozenset({
    "planned",
    "implemented",
    "reviewed",
    "replanning",
    "replanned",
    "iteration_failed",
    "waiting_for_session",
    "log",                # only when payload.msg starts with "subtask "
    "skill_suggested",
    "done",
    "failed",
    "cancelled",
})


def _fmt_wait_duration(secs: int) -> str:
    if secs >= 86400:
        return f"~{secs // 86400}d {(secs % 86400) // 3600}h"
    if secs >= 3600:
        return f"~{secs // 3600}h {(secs % 3600) // 60}min"
    if secs >= 60:
        return f"~{secs // 60}min {secs % 60}s"
    return f"{secs}s"


def format_milestone(event: str, payload: dict, lang: str = "de") -> list[str]:
    """Convert one progress event into 0-N display lines (plain text).

    Returns `[]` for non-milestone events, so callers can naively call
    this for every log line and filter via truthiness.
    """
    if event not in _MILESTONE_EVENTS:
        return []
    if not isinstance(payload, dict):
        payload = {}

    if event == "planned":
        steps_n = len(payload.get("steps") or [])
        summary = (payload.get("summary") or "").strip()
        subs = payload.get("subtasks") or []
        subs_n = payload.get("subtasks_count") or len(subs)
        head = (
            f"📋 Plan ready — {steps_n} steps"
            + (f", {subs_n} sub-tasks" if subs_n else "")
        ) if lang != "de" else (
            f"📋 Plan steht — {steps_n} Steps"
            + (f", {subs_n} Sub-Tasks" if subs_n else "")
        )
        lines = [head]
        if summary:
            lines.append(f"   {summary[:240]}")
        if subs:
            label = "Sub-Task-Reihenfolge" if lang == "de" else "Sub-task order"
            lines.append(f"   🪓 {label}:")
            for i, name in enumerate(subs[:8]):
                lines.append(f"     {i + 1}. {name}")
            if len(subs) > 8:
                lines.append(f"     … +{len(subs) - 8} more")
        return lines

    if event == "log":
        msg = (payload.get("msg") or "").strip()
        kind = payload.get("kind") or ""
        if msg.startswith("subtask "):
            return [f"🪓 {msg}"]
        if kind == "stuck-alert":
            return [f"⚠️  {msg}"]
        if kind == "permission-issue":
            return [f"🔒 {msg}"]
        if kind == "implementer-stuck":
            return [f"🔁 {msg}"]
        return []

    if event == "implemented":
        iter_n = payload.get("iteration") or "?"
        ops = payload.get("ops") or 0
        failed = payload.get("failed") or 0
        sub = payload.get("subtask")
        suffix = f" [{sub}]" if sub else ""
        if failed:
            return [f"🔧 iter {iter_n} — {ops} ops, {failed} failed{suffix}"]
        return [f"🔧 iter {iter_n} — {ops} ops{suffix}"]

    if event == "reviewed":
        iter_n = payload.get("iteration") or "?"
        passed = bool(payload.get("pass"))
        sub = payload.get("subtask")
        suffix = f" [{sub}]" if sub else ""
        if passed:
            if sub:
                return [(
                    f"✅ Sub-task {sub} complete (iter {iter_n})"
                    if lang != "de"
                    else f"✅ Sub-Task {sub} abgeschlossen (Iter {iter_n})"
                )]
            return [f"✅ iter {iter_n} review pass{suffix}"]
        feedback = (payload.get("feedback") or "").strip()
        first_line = feedback.split("\n", 1)[0][:160] if feedback else ""
        out = [f"❌ iter {iter_n} review fail{suffix}"]
        if first_line:
            out.append(f"   ↳ {first_line}")
        return out

    if event == "replanning":
        replans = (payload.get("replans_done") or 0) + 1
        after = payload.get("after_iteration", "?")
        return [(
            f"🔄 Replanning #{replans} after iter {after}…"
            if lang != "de"
            else f"🔄 Re-Plan #{replans} nach Iter {after}…"
        )]

    if event == "replanned":
        summary = (payload.get("summary") or "").strip()[:200]
        checks = payload.get("checks") or []
        out = [(
            f"✅ New plan: {summary}"
            if lang != "de" else f"✅ Neuer Plan: {summary}"
        )]
        if checks:
            out.append(f"   checks: {', '.join(checks[:5])}")
        return out

    if event == "iteration_failed":
        iter_n = payload.get("iteration") or "?"
        feedback = (payload.get("feedback") or "").strip()
        first_line = feedback.split("\n", 1)[0][:200] if feedback else ""
        out = [f"❌ iter {iter_n} failed"]
        if first_line:
            out.append(f"   ↳ {first_line}")
        return out

    if event == "waiting_for_session":
        secs = int(payload.get("seconds") or 0)
        attempt = int(payload.get("attempt") or 1)
        reason = (payload.get("reason") or "").strip()[:120]
        task_id = payload.get("task_id") or ""
        when = _fmt_wait_duration(secs)
        head = (
            f"⏳ Waiting for next session window (attempt {attempt}) — {when}"
            if lang != "de"
            else f"⏳ Warte auf nächste Session (Versuch {attempt}) — {when}"
        )
        out = [head]
        if reason:
            out.append(f"   ↳ {reason}")
        if task_id:
            tip = (
                f"   💡 Live-switch provider: /cancel {task_id} → /models → /resume {task_id}"
                if lang != "de"
                else f"   💡 Live-Switch Provider: /cancel {task_id} → /models → /resume {task_id}"
            )
            out.append(tip)
        return out

    if event == "skill_suggested":
        name = payload.get("name") or "?"
        desc = (payload.get("description") or "").strip()[:120]
        return [
            f"💡 Skill suggestion: {name}",
            f"   {desc}" if desc else "",
        ][:2 if desc else 1]

    if event == "done":
        summary = (payload.get("summary") or "").strip()[:160]
        return [f"✅ Done — {summary}" if lang != "de" else f"✅ Fertig — {summary}"]

    if event == "failed":
        reason = (payload.get("reason") or "").strip()[:160]
        feedback = (payload.get("feedback") or "").strip()[:160]
        out = ["❌ Failed" if lang != "de" else "❌ Fehlgeschlagen"]
        if reason:
            out.append(f"   reason: {reason}")
        if feedback:
            out.append(f"   ↳ {feedback}")
        return out

    if event == "cancelled":
        return ["🚫 Cancelled" if lang != "de" else "🚫 Abgebrochen"]

    return []


def parse_log_message(msg: str) -> tuple[str, dict] | None:
    """Reverse of `_emit`'s log format: `"<event>: <json-payload>"`.

    Returns `(event, payload)` or `None` if the line doesn't match
    (e.g. raw `_log()` writes that didn't go through `_emit`).
    Payloads may be truncated to 300 chars by `_emit`, so JSON parse
    failures fall back to an empty dict so the event name still
    surfaces — better a one-line milestone than nothing.
    """
    if not msg or ":" not in msg:
        return None
    event, _, rest = msg.partition(": ")
    event = event.strip()
    rest = rest.strip()
    if not event or not rest:
        return None
    # Some `_log()` calls produce messages like
    # "resume: iteration 0 plan was corrupt" — that's not an event.
    # We only treat it as event if the rest looks like a JSON object.
    if not rest.startswith("{"):
        return None
    try:
        payload = json.loads(rest)
        if isinstance(payload, dict):
            return event, payload
    except Exception:
        return event, {}
    return None
