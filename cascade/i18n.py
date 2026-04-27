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
            "*🌊 Cascade-Bot*\n"
            "_Multi-Agent Coding-Bot: Plan → Implement → Review_\n\n"

            "*▸ Eingabe*\n"
            "• *Text* → Aufgabe oder Frage; ich entscheide selbst was passiert.\n"
            "• *Voice* → Whisper-Transkription, dann wie Text.\n"
            "• *Foto / Dokument + Caption* → Caption = Aufgabe, Datei = Anhang (Vision).\n\n"

            "*▸ Architektur*\n"
            "1. *Triage (3-Modi)* — Chat / Direct-Action / volle Cascade. "
            "Pfade werden gegen Allowlist vorvalidiert; bei Datei-Klassifikation "
            "(JSON/SA/OAuth) Auto-Stage ohne Nachfrage wenn sicher.\n"
            "2. *Planner* — Steps + Acceptance + Quality-Checks (Opus default, "
            "DE-Prompt wenn lang=de). Erkennt trivial-Tasks und schreibt direct_ops.\n"
            "3. *Loop* — Implementer → Quality-Checks → Reviewer; max-iterations "
            "ist standardmäßig ∞ (nur Usage stoppt). Replan-Budget per /replan (default 2). "
            "*Stagnation-Detection*: 2× identisches Reviewer-Feedback → sofortiger "
            "Replan; mit erschöpftem Budget → Run endet sauber statt 999× zu loopen.\n"
            "4. *Self-Healing* — `with_retry` wartet bei Rate-Limits / "
            "weekly-Session-Caps automatisch (Default 7 Tage Budget). "
            "*HealingMonitor* erkennt stuck-Phasen, permission-denied im Log und "
            "3× identische Implementer-Outputs.\n"
            "5. *External Context* — Library-Docs (Context7) + Web-Hits (Brave) "
            "werden automatisch geladen wenn nötig.\n\n"

            "*▸ Gedächtnis*\n"
            "• *Hot-Tier*: letzte 30 Nachrichten verbatim, mit Datei-Inhalt inline "
            "(bis 30 KB pro Upload, Klassifikation z.B. google_service_account).\n"
            "• *Warm-Tier*: ältere Nachrichten → Sonnet-Summarization alle 6h "
            "(`chat_summaries`).\n"
            "• *Long-Tier*: RLM (BM25 Ranking + DE/EN-Stopwords + Importance-Boost).\n"
            "• `build_context()` baut USER FACTS · RECENT UPLOADS · CONVERSATION · "
            "EARLIER · SEARCH HITS für jeden Triage-Call.\n"
            "• `/forget` → wischt Chat-Verlauf, Summaries und Pending-Attachments.\n\n"

            "*▸ Task-Kontrolle*\n"
            "/status `[id]` — Status (default: letzter Task)\n"
            "/logs `[id]` — letzte 50 Log-Zeilen\n"
            "/diff `[id]` — kompletter Diff\n"
            "/history — letzte 10 Tasks\n"
            "/queue — was läuft gerade\n"
            "/wait — wer wartet gerade auf Rate-Limit / Session-Window + ETA\n"
            "/cancel `[id]` — laufenden Task abbrechen (auch Orphans im DB)\n"
            "/abort — ALLES laufende killen + DB-Orphans aufräumen\n"
            "/dryrun `<task>` — nur planen, nichts ausführen (billig)\n\n"

            "*▸ Wieder anfassen (failed Tasks)*\n"
            "/again `[id]` — *Fresh-Restart* (neue task_id, neuer Workspace) — "
            "bei failed wird der letzte Reviewer-Hint als 'Lessons Learned' "
            "in den Plan-Prompt gelegt.\n"
            "/resume `<id>` — *Weiter im selben Sandkasten*: gleiche task_id, "
            "gleicher Workspace, weiter ab letzter Iteration. Replan-Budget startet frisch.\n"
            "/resume `<id>` `<extra-text>` — wie /resume, aber der extra Text wird als "
            "*zusätzlicher Hinweis* in den Resume-Run geschickt. _Beispiel:_ "
            "`/resume bdce20… nutze python3 statt python und füge tests hinzu`\n\n"

            "*▸ Live-Switch während Cascade hängt (Provider-Wechsel)*\n"
            "Cloud-LLM-Errors werden bis zu 7 Tage lang im 1h-Takt automatisch "
            "retried. Wenn du in der Zwischenzeit auf einen anderen Provider "
            "(z.B. von Ollama Cloud auf Claude) wechseln willst:\n"
            "1. `/cancel <id>` — bricht den Wait-Sleep sofort ab\n"
            "2. `/models` — neuen Worker auswählen\n"
            "3. `/resume <id>` — macht ab letzter Iteration mit neuem Model weiter\n\n"

            "*▸ Skills (wiederverwendbare Templates)*\n"
            "/skills — Liste (werden nach erfolgreichen Runs vorgeschlagen)\n"
            "/run `<name>` — Skill ausführen\n"
            "/skillupgrade — Opus geht alle Skills durch, optimiert auf Basis der "
            "letzten Tasks; bei Mehrdeutigkeit fragt der Bot zurück.\n\n"

            "*▸ Erstmaliges Setup*\n"
            "/setup — geführter Wizard für API-Keys (Ollama / OpenAI-compat / "
            "Whisper / Brave / GitHub PAT). Schreibt in `secrets.env` "
            "(gitignored, chmod 0600). Deine `.env` wird *nicht* angefasst.\n\n"

            "*▸ Konfig (per Chat persistiert)*\n"
            "/settings — alle aktuellen Werte\n"
            "/repo `<pfad>` — Default-Repo (`clear` zum Löschen)\n"
            "/lang `<de|en>` — Sprache\n"
            "/models — Modell pro Worker (Buttons)\n"
            "/effort — Effort (Claude) bzw. Temperature (Ollama) pro Worker\n"
            "/replan `[n]` — Replan-Budget 0..999 (default 2; 999 = ∞)\n"
            "/iterations `[n]` — Max-Iterationen pro Run (default ∞; nur Usage stoppt)\n"
            "/failsbeforereplan `[n]` — Fails vor Auto-Replan (default 2)\n"
            "/subtasks `[n]` — Max Sub-Tasks beim Auto-Decompose (default 6)\n"
            "/toggles — Triage / Auto-Skill / Context7 / Web-Suche / Auto-Decompose / Multi-Plan an/aus\n"
            "/forget — Chat-Verlauf reset\n"
            "/chat — welches Chat-Modell ist gerade aktiv (Ground Truth)\n\n"

            "*▸ System*\n"
            "/projects — alle lokalen Repos & Workspaces (mit Größe)\n"
            "/errors `[n]` — letzte n Fehler aus dem Bot-Log (default 5)\n"
            "/exec `<cmd>` — Shell (60s Timeout, 4 kB Output-Cap)\n"
            "/git `<repo>` `<subcmd>` — git (status/log/diff/branch/checkout/pull/push/commit)\n"
            "/whoami — Bot/Owner-Info\n"
            "/help — diese Übersicht\n\n"

            "_Tip: schreib drauflos — der Bot triagiert selbst._"
        ),
        "en": (
            "*🌊 Cascade-Bot*\n"
            "_Multi-agent coding bot: Plan → Implement → Review_\n\n"

            "*▸ Input*\n"
            "• *Text* → task or question; I decide what to do.\n"
            "• *Voice* → Whisper transcription, then treated as text.\n"
            "• *Photo / document + caption* → caption = task, file = attachment (vision).\n\n"

            "*▸ Architecture*\n"
            "1. *Triage (3 modes)* — chat / direct-action / full cascade. "
            "Paths are pre-validated against the allowlist; recognized JSON "
            "credentials are auto-staged when safe.\n"
            "2. *Planner* — steps + acceptance + quality checks (Opus default, "
            "DE prompt when lang=de). Detects trivial tasks → direct_ops shortcut.\n"
            "3. *Loop* — Implementer → quality checks → Reviewer; max-iterations "
            "is unlimited by default (only usage stops). Replan budget via /replan "
            "(default 2). *Stagnation detection*: identical reviewer feedback 2× → "
            "force replan; with budget exhausted → run ends cleanly instead of "
            "looping 999 times.\n"
            "4. *Self-healing* — `with_retry` waits automatically on rate-limits / "
            "weekly-session caps (default 7-day budget). *HealingMonitor* surfaces "
            "stuck phases, permission-denied diagnostics, and 3× identical "
            "implementer outputs.\n"
            "5. *External context* — library docs (Context7) + web hits (Brave) are "
            "auto-fetched when relevant.\n\n"

            "*▸ Memory*\n"
            "• *Hot tier*: last 30 messages verbatim, with file content inlined "
            "(up to 30 KB per upload, classified e.g. google_service_account).\n"
            "• *Warm tier*: older messages → Sonnet summarisation every 6h "
            "(`chat_summaries`).\n"
            "• *Long tier*: RLM (BM25 ranking + DE/EN stop-words + importance boost).\n"
            "• `build_context()` ships USER FACTS · RECENT UPLOADS · CONVERSATION · "
            "EARLIER · SEARCH HITS into every triage call.\n"
            "• `/forget` → wipes chat history, summaries, and pending attachments.\n\n"

            "*▸ Task control*\n"
            "/status `[id]` — status (default: latest task)\n"
            "/logs `[id]` — last 50 log lines\n"
            "/diff `[id]` — full diff\n"
            "/history — last 10 tasks\n"
            "/queue — what's running\n"
            "/wait — who is waiting on a rate-limit / session window + ETA\n"
            "/cancel `[id]` — cancel a running task (handles DB orphans too)\n"
            "/abort — kill EVERYTHING running + sweep DB orphans\n"
            "/dryrun `<task>` — plan only, don't execute (cheap)\n\n"

            "*▸ Retrying (failed tasks)*\n"
            "/again `[id]` — *fresh restart* (new task_id, new workspace); "
            "on failed status, the last reviewer feedback is injected as "
            "\"lessons learned\" into the new plan prompt.\n"
            "/resume `<id>` — *continue same sandbox*: same task_id, same workspace, "
            "next iteration. Replan budget resets.\n"
            "/resume `<id>` `<extra-text>` — same as /resume, but the extra text is "
            "appended as an *additional hint* into the resumed run. _Example:_ "
            "`/resume bdce20… use python3 instead of python and add tests`\n\n"

            "*▸ Live-switch while a cascade is waiting (provider swap)*\n"
            "Cloud-LLM errors are retried automatically every 1h for up to 7 "
            "days. If you'd rather swap to a different provider (e.g. Ollama "
            "Cloud → Claude) while it's stuck:\n"
            "1. `/cancel <id>` — breaks the wait-sleep immediately\n"
            "2. `/models` — pick a different worker\n"
            "3. `/resume <id>` — continues from the last iteration with the new model\n\n"

            "*▸ Skills (reusable templates)*\n"
            "/skills — list (auto-suggested after successful runs)\n"
            "/run `<name>` — run a skill\n"
            "/skillupgrade — Opus walks every skill, optimises based on recent "
            "tasks; asks back if the answer is ambiguous.\n\n"

            "*▸ First-time setup*\n"
            "/setup — guided wizard for API keys (Ollama / OpenAI-compat / "
            "Whisper / Brave / GitHub PAT). Writes to `secrets.env` "
            "(gitignored, chmod 0600). Your `.env` stays untouched.\n\n"

            "*▸ Config (per-chat, persisted)*\n"
            "/settings — all current values\n"
            "/repo `<path>` — default repo (`clear` to remove)\n"
            "/lang `<de|en>` — language\n"
            "/models — model per worker (buttons)\n"
            "/effort — effort (Claude) or temperature (Ollama) per worker\n"
            "/replan `[n]` — replan budget 0..999 (default 2; 999 = ∞)\n"
            "/iterations `[n]` — max iterations per run (default ∞; only usage stops)\n"
            "/failsbeforereplan `[n]` — fails before auto-replan (default 2)\n"
            "/subtasks `[n]` — max sub-tasks for auto-decompose (default 6)\n"
            "/toggles — triage / auto-skill / Context7 / web search / auto-decompose / multi-plan on/off\n"
            "/forget — clear chat history\n"
            "/chat — which chat model is active right now (ground truth)\n\n"

            "*▸ System*\n"
            "/projects — list local repos & workspaces (with size)\n"
            "/errors `[n]` — last n errors from the bot log (default 5)\n"
            "/exec `<cmd>` — shell (60s timeout, 4 kB output cap)\n"
            "/git `<repo>` `<subcmd>` — git (status/log/diff/branch/checkout/pull/push/commit)\n"
            "/whoami — bot/owner info\n"
            "/help — this overview\n\n"

            "_Tip: just write — the bot triages itself._"
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


def de_en(de: str, en: str, lang: str = "de") -> str:
    """Pick a German or English string by `lang`.

    Cuts down on the ~150 ad-hoc `"X" if lang == "de" else "Y"` ternaries
    sprinkled across the bot — most of them are inline strings that don't
    deserve their own i18n key, but the ternary makes long expressions
    unreadable. `de_en("Antwort", "answer", lang)` reads cleanly.

    Default falls back to German when `lang` is anything other than "en"
    (defensive — covers `None`, empty string, accidental "EN", etc.).
    """
    return en if lang == "en" else de


def t(key: str, *, lang: Lang = "de", **vars) -> str:
    entry = _STRINGS.get(key)
    if not entry:
        return f"[missing:{key}]"
    template = entry.get(lang) or entry.get("en") or next(iter(entry.values()))
    try:
        return template.format(**vars)
    except KeyError as e:
        return f"[fmt-error {key}: missing {e}]"
