"""Tiny i18n for the Telegram bot. Keys → {lang: template}.

Use `t("key", lang=..., **vars)`. Templates use Python str.format().
"""

from __future__ import annotations

from typing import Literal

Lang = Literal["de", "en"]

_STRINGS: dict[str, dict[Lang, str]] = {
    # progress events
    "progress.planning_initial": {
        "de": "🧠 Plane …",
        "en": "🧠 Planning…",
    },
    "progress.planning": {"de": "  → plane …", "en": "  → planning…"},
    "progress.planned": {"de": "  ✓ Plan: {summary}", "en": "  ✓ plan: {summary}"},
    "progress.implementing": {
        "de": "  → Iteration {n} implementiert …",
        "en": "  → iter {n} implementing…",
    },
    "progress.implemented": {
        "de": "  ✓ Iteration {n}: {ops} Ops ({failed} fehlgeschlagen)",
        "en": "  ✓ iter {n}: {ops} ops ({failed} failed)",
    },
    "progress.reviewing": {
        "de": "  → Iteration {n} prüfe …",
        "en": "  → iter {n} reviewing…",
    },
    "progress.reviewed_pass": {
        "de": "  ✅ Iteration {n} Review{suffix}",
        "en": "  ✅ iter {n} review{suffix}",
    },
    "progress.reviewed_fail": {
        "de": "  ❌ Iteration {n} Review{suffix}",
        "en": "  ❌ iter {n} review{suffix}",
    },
    "progress.failed": {
        "de": "  ❌ fehlgeschlagen: {summary}",
        "en": "  ❌ failed: {summary}",
    },
    # final result
    "result.summary": {
        "de": "{emoji} *{status}* — `{task_id}`\nIterationen: {iterations}\nWorkspace: `{workspace}`\nZusammenfassung: {summary}",
        "en": "{emoji} *{status}* — `{task_id}`\niterations: {iterations}\nworkspace: `{workspace}`\nsummary: {summary}",
    },
    "result.cancelled": {"de": "🚫 Abgebrochen.", "en": "🚫 Cancelled."},
    "result.crashed": {"de": "❌ Cascade abgestürzt: {error}", "en": "❌ Cascade crashed: {error}"},
    # common
    "no_tasks": {"de": "Noch keine Tasks.", "en": "No tasks yet."},
    "no_logs": {"de": "(keine Logs)", "en": "(no logs)"},
    "no_inflight": {"de": "Nichts in Bearbeitung.", "en": "Nothing in flight."},
    "cancel_sent": {
        "de": "🚫 Cancel-Signal an `{task_id}` gesendet.",
        "en": "🚫 Cancel signal sent to `{task_id}`.",
    },
    "cancel_not_running": {
        "de": "Task `{task_id}` läuft nicht in diesem Prozess.",
        "en": "Task `{task_id}` not running in this process.",
    },
    "task_not_found": {
        "de": "Task `{task_id}` nicht gefunden.",
        "en": "Task `{task_id}` not found.",
    },
    "status_line": {
        "de": "{emoji} {status} — `{task_id}`\nTask: {task}\nIter: {iteration}\nZusammenfassung: {summary}",
        "en": "{emoji} {status} — `{task_id}`\ntask: {task}\niter: {iteration}\nsummary: {summary}",
    },
    # /repo
    "repo.current": {"de": "Aktuelles Repo: `{path}`", "en": "Current repo: `{path}`"},
    "repo.cleared": {
        "de": "Repo gelöscht. Neue Tasks nutzen ein Tmp-Workspace.",
        "en": "Repo cleared. New tasks use a tmp workspace.",
    },
    "repo.set": {"de": "Repo gesetzt: `{path}`", "en": "Repo set: `{path}`"},
    "repo.not_found": {"de": "Pfad nicht gefunden: `{path}`", "en": "Path not found: `{path}`"},
    # /resume
    "resume.usage": {"de": "Aufruf: /resume <task_id>", "en": "Usage: /resume <task_id>"},
    # /exec
    "exec.usage": {"de": "Aufruf: /exec <cmd …>", "en": "Usage: /exec <cmd …>"},
    "exec.timeout": {
        "de": "⏱ Timeout nach 60s",
        "en": "⏱ timed out after 60s",
    },
    "exec.no_output": {"de": "(keine Ausgabe)", "en": "(no output)"},
    # /git
    "git.usage": {"de": "Aufruf: /git <repo> <subcmd …>", "en": "Usage: /git <repo> <subcmd …>"},
    "git.not_whitelisted": {
        "de": "git-Subcommand `{sub}` nicht erlaubt. Whitelist: {whitelist}",
        "en": "git subcommand `{sub}` not in whitelist: {whitelist}",
    },
    "git.not_a_repo": {"de": "Kein Git-Repo: `{path}`", "en": "Not a git repo: `{path}`"},
    # voice
    "voice.no_key": {
        "de": "OPENAI_API_KEY nicht gesetzt — Voice-Transkription nicht möglich.",
        "en": "OPENAI_API_KEY not set — cannot transcribe voice.",
    },
    "voice.empty": {"de": "(leere Transkription)", "en": "(empty transcription)"},
    "voice.transcript": {"de": "📝 _{text}_", "en": "📝 _{text}_"},
    # photo
    "photo.no_caption": {
        "de": "Bitte eine Bildunterschrift hinzufügen, die beschreibt was zu tun ist.",
        "en": "Please add a caption describing what to do with this attachment.",
    },
    # startup
    "startup.interrupted": {
        "de": "🔁 Bot neu gestartet. {n} Task(s) als unterbrochen markiert: {ids}\nMit /resume <id> fortsetzen.",
        "en": "🔁 Bot restarted. {n} task(s) marked interrupted: {ids}\nUse /resume <id> to continue.",
    },
    # /help
    "help": {
        "de": (
            "*Claude-Cascade Bot*\n\n"
            "Schicke Text/Voice/Foto/Dokument → Cascade-Run.\n"
            "Schicke eine Frage → ich antworte, ohne zu starten.\n\n"
            "*Tasks*\n"
            "/again [id]    — letzten/spez. Task neu starten\n"
            "/dryrun <task> — nur planen, nichts ausführen (cheap)\n"
            "/status [id]   — Task-Status\n"
            "/diff [id]     — kompletten Diff zeigen\n"
            "/logs [id]     — letzte 50 Log-Zeilen\n"
            "/history       — letzte 10 Tasks\n"
            "/queue         — laufende Tasks\n"
            "/cancel [id]   — laufenden Task abbrechen\n"
            "/abort         — alle laufenden abbrechen\n"
            "/resume <id>   — unterbrochenen fortsetzen\n\n"
            "*Skills*\n"
            "/skills        — gespeicherte Skills (auto nach Runs vorgeschlagen)\n"
            "/run <name>    — Skill ausführen\n\n"
            "*Konfig*\n"
            "/settings      — alle aktuellen Einstellungen\n"
            "/repo <pfad>   — Default-Repo (`clear` zum Löschen)\n"
            "/lang <de|en>  — Sprache\n"
            "/models        — Modell pro Worker\n"
            "/effort        — Effort-Stufe pro Worker\n"
            "/replan [n]    — Replan-Budget (0..10)\n\n"
            "*System*\n"
            "/projects      — alle lokalen Repos & Workspaces zeigen (mit Größe)\n"
            "/exec <cmd>    — Shell (60s, 4kB-Cap)\n"
            "/git <repo> <subcmd …>  — git (Whitelist)\n"
            "/whoami        — Bot-/Owner-Info\n"
            "/help          — diese Übersicht"
        ),
        "en": (
            "*Claude-Cascade Bot*\n\n"
            "Send text/voice/photo/document → cascade run.\n"
            "Send a question → I'll reply without starting.\n\n"
            "*Tasks*\n"
            "/again [id]    — re-run last / specified task\n"
            "/dryrun <task> — plan only, don't execute (cheap)\n"
            "/status [id]   — task status\n"
            "/diff [id]     — show full diff\n"
            "/logs [id]     — last 50 log lines\n"
            "/history       — last 10 tasks\n"
            "/queue         — in-flight tasks\n"
            "/cancel [id]   — cancel a running task\n"
            "/abort         — cancel all running\n"
            "/resume <id>   — resume an interrupted task\n\n"
            "*Skills*\n"
            "/skills        — list saved skills (auto-suggested after runs)\n"
            "/run <name>    — run a saved skill\n\n"
            "*Config*\n"
            "/settings      — all current settings\n"
            "/repo <path>   — default repo (`clear` to remove)\n"
            "/lang <de|en>  — language\n"
            "/models        — model per worker\n"
            "/effort        — effort level per worker\n"
            "/replan [n]    — replan budget (0..10)\n\n"
            "*System*\n"
            "/projects      — list local repos & workspaces (with size)\n"
            "/exec <cmd>    — shell (60s, 4kB cap)\n"
            "/git <repo> <subcmd …>  — git (whitelist)\n"
            "/whoami        — bot/owner info\n"
            "/help          — this message"
        ),
    },
    # /lang
    "lang.usage": {
        "de": "Aufruf: /lang <de|en>. Aktuell: `{current}`",
        "en": "Usage: /lang <de|en>. Current: `{current}`",
    },
    "lang.set": {
        "de": "Sprache auf `{lang}` umgestellt.",
        "en": "Language switched to `{lang}`.",
    },
}


def t(key: str, *, lang: Lang = "de", **vars) -> str:
    entry = _STRINGS.get(key)
    if not entry:
        return f"[missing:{key}]"
    template = entry.get(lang) or entry.get("en") or next(iter(entry.values()))
    try:
        return template.format(**vars)
    except KeyError as e:
        return f"[fmt-error {key}: missing {e}]"
