"""General commands: /help /start /whoami /settings + the catch-all unknown."""

from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode

from cascade.config import settings
from cascade.i18n import de_en, t
from cascade.store import Store

from ..helpers import lang_for, owner_only


async def cmd_help(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    # /help has grown past Telegram's 4096-char single-message cap; use
    # send_long which auto-chunks while preserving markdown.
    from ..helpers import send_long
    await send_long(
        update.effective_message,
        t("help", lang=lang_for(update)),
    )


async def cmd_start(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    # Detect first-time / unconfigured installs and nudge to /setup.
    from cascade.bot.handlers.setup import is_setup_required
    needs_setup = is_setup_required()

    if lang == "de":
        text = (
            "👋 *Willkommen beim Cascade-Bot*\n\n"
            "Ich bin ein Multi-Agent Coding-Bot mit drei Layern:\n"
            "  • *Triage* — Chat / Direkt-Aktion / volle Cascade\n"
            "  • *Plan → Implement → Review* — bis Quality-Checks alle ✅\n"
            "  • *Self-Healing* — Stagnation-Detection, Auto-Wait bei Rate-Limits\n\n"
            "*So nutzt du mich:*\n"
            "  • Schreib eine Aufgabe (Text/Voice/Foto+Caption) — ich plane, baue, prüfe.\n"
            "  • Frag eine Frage — ich antworte ohne Cascade.\n"
            "  • `/help` zeigt alle Commands."
        )
        if needs_setup:
            text += (
                "\n\n⚠️ *Setup nötig.*\n"
                "Es ist noch kein Implementer-API-Key konfiguriert.\n"
                "Starte den geführten Setup-Prozess mit `/setup`.\n\n"
                "Werte landen in `secrets.env` (gitignored, chmod 0600) — "
                "dein `.env` bleibt unangetastet."
            )
        else:
            text += "\n\n_Beispiel: „Erstelle eine kleine CLI in /tmp/foo die `--version` ausgibt"
    else:
        text = (
            "👋 *Welcome to Cascade-Bot*\n\n"
            "I'm a multi-agent coding bot with three layers:\n"
            "  • *Triage* — chat / direct-action / full cascade\n"
            "  • *Plan → Implement → Review* — until every quality check ✅\n"
            "  • *Self-healing* — stagnation detection, auto-wait on rate-limits\n\n"
            "*How to use me:*\n"
            "  • Send a task (text/voice/photo+caption) — I plan, build, review.\n"
            "  • Ask a question — I'll reply without spinning up a cascade.\n"
            "  • `/help` lists every command."
        )
        if needs_setup:
            text += (
                "\n\n⚠️ *Setup required.*\n"
                "No implementer API key is configured yet.\n"
                "Run the guided setup with `/setup`.\n\n"
                "Values are written to `secrets.env` (gitignored, chmod 0600) — "
                "your `.env` is left alone."
            )
        else:
            text += "\n\n_Example: \"Create a small CLI in /tmp/foo that prints --version\"_"
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_whoami(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    s = settings()
    user = update.effective_user
    chat = update.effective_chat
    lang = lang_for(update)
    bot_me = await ctx.bot.get_me()
    if lang == "de":
        text = (
            f"*Bot:* @{bot_me.username} (`{bot_me.id}`)\n"
            f"*Owner:* `{s.telegram_owner_id}`\n"
            f"*Du bist:* {user.first_name} (`{user.id}`)\n"
            f"*Chat:* `{chat.id}`\n"
            f"*Sprache:* `{lang}`\n"
            f"*Zeitzone:* `{s.cascade_timezone}`"
        )
    else:
        text = (
            f"*Bot:* @{bot_me.username} (`{bot_me.id}`)\n"
            f"*Owner:* `{s.telegram_owner_id}`\n"
            f"*You are:* {user.first_name} (`{user.id}`)\n"
            f"*Chat:* `{chat.id}`\n"
            f"*Language:* `{lang}`\n"
            f"*Timezone:* `{s.cascade_timezone}`"
        )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_settings(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    chat_id = update.effective_chat.id
    sess = await store.get_chat_session(chat_id) or {}
    s = settings()

    repo = sess.get("repo_path") or "—"
    plan_m = sess.get("planner_model") or s.cascade_planner_model
    impl_m = sess.get("implementer_model") or s.cascade_implementer_model
    rev_m = sess.get("reviewer_model") or s.cascade_reviewer_model
    chat_m = sess.get("chat_model") or s.cascade_triage_model
    plan_e = sess.get("planner_effort") or s.cascade_planner_effort or "default"
    rev_e = sess.get("reviewer_effort") or s.cascade_reviewer_effort or "default"
    tri_e = sess.get("triage_effort") or s.cascade_triage_effort or "default"

    def _budget(v: int | None, default: int) -> str:
        if v is None:
            return f"default ({default if default < 999 else '∞'})"
        return "∞" if v >= 999 else str(v)

    replan_display = _budget(sess.get("replan_max"), s.cascade_replan_max)
    iter_display = _budget(sess.get("max_iterations"), s.cascade_max_iterations)
    rfail_display = _budget(sess.get("replan_after_failures"), s.cascade_replan_after_failures)

    def _toggle(col: str, attr: str) -> str:
        v = sess.get(col)
        eff = bool(v) if v is not None else bool(getattr(s, attr))
        return "on" if eff else "off"

    triage = _toggle("triage_enabled", "cascade_triage_enabled")
    auto_skill = _toggle("auto_skill_suggest", "cascade_auto_skill_suggest")
    ctx7 = _toggle("context7_enabled", "cascade_context7_enabled")
    websearch = _toggle("websearch_enabled", "cascade_websearch_enabled")

    if lang == "de":
        text = (
            "⚙️ *Aktuelle Chat-Einstellungen*\n\n"
            f"*Repo:* `{repo}`\n"
            f"*Sprache:* `{lang}`\n\n"
            "*Modelle:*\n"
            f"• 🧠 Planner: `{plan_m}` (effort `{plan_e}`)\n"
            f"• 🛠 Implementer: `{impl_m}`\n"
            f"• 🔍 Reviewer: `{rev_m}` (effort `{rev_e}`)\n"
            f"• 💬 Chat: `{chat_m}` (effort `{tri_e}`)\n\n"
            "*Run-Budget:*\n"
            f"• Max-Iterationen: `{iter_display}`\n"
            f"• Replan-Budget: `{replan_display}`\n"
            f"• Fails vor Auto-Replan: `{rfail_display}`\n\n"
            "*Features:*\n"
            f"• 🧭 Triage: `{triage}`\n"
            f"• 💡 Auto-Skill-Vorschläge: `{auto_skill}`\n"
            f"• 📚 Context7: `{ctx7}`\n"
            f"• 🌐 Web-Suche: `{websearch}`\n\n"
            "Ändern via /repo /lang /models /effort /replan /iterations "
            "/failsbeforereplan /toggles."
        )
    else:
        text = (
            "⚙️ *Current chat settings*\n\n"
            f"*Repo:* `{repo}`\n"
            f"*Language:* `{lang}`\n\n"
            "*Models:*\n"
            f"• 🧠 Planner: `{plan_m}` (effort `{plan_e}`)\n"
            f"• 🛠 Implementer: `{impl_m}`\n"
            f"• 🔍 Reviewer: `{rev_m}` (effort `{rev_e}`)\n"
            f"• 💬 Chat: `{chat_m}` (effort `{tri_e}`)\n\n"
            "*Run budget:*\n"
            f"• Max iterations: `{iter_display}`\n"
            f"• Replan budget: `{replan_display}`\n"
            f"• Fails before auto-replan: `{rfail_display}`\n\n"
            "*Features:*\n"
            f"• 🧭 Triage: `{triage}`\n"
            f"• 💡 Auto-skill suggestions: `{auto_skill}`\n"
            f"• 📚 Context7: `{ctx7}`\n"
            f"• 🌐 Web search: `{websearch}`\n\n"
            "Change via /repo /lang /models /effort /replan /iterations "
            "/failsbeforereplan /toggles."
        )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_chat(update: Update, ctx) -> None:
    """Show which model the chat/triage layer is currently using.
    Verifies the truth from settings, not by asking the LLM (which
    happily hallucinates its own name)."""
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    s = settings()
    store: Store = ctx.application.bot_data["store"]
    chat_id = update.effective_chat.id
    sess = await store.get_chat_session(chat_id) or {}

    model = sess.get("chat_model") or s.cascade_triage_model
    provider = "Claude (CLI)" if model.startswith("claude-") else "Ollama Cloud"
    effort = sess.get("triage_effort") or s.cascade_triage_effort or "default"
    enabled = "on" if s.cascade_triage_enabled else "off"
    source = "per-Chat" if sess.get("chat_model") else "Default"

    if lang == "de":
        text = (
            "*💬 Aktuelles Chat-Modell*\n\n"
            f"• Modell: `{model}`\n"
            f"• Provider: {provider}\n"
            f"• Quelle: {source}\n"
            f"• Effort: `{effort}`\n"
            f"• Triage aktiv: `{enabled}`\n\n"
            "_Wechseln via_ /models _→ 💬 Chat. Hinweis: das Modell weiß "
            "selbst nicht zuverlässig welche Version es ist — diese Anzeige "
            "ist die Wahrheit aus der Bot-Config._"
        )
    else:
        text = (
            "*💬 Current chat model*\n\n"
            f"• Model: `{model}`\n"
            f"• Provider: {provider}\n"
            f"• Source: {source}\n"
            f"• Effort: `{effort}`\n"
            f"• Triage enabled: `{enabled}`\n\n"
            "_Switch via_ /models _→ 💬 Chat. Note: the model itself doesn't "
            "reliably know its own version — this readout is the ground truth "
            "from the bot config._"
        )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_forget(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    n = await store.clear_chat_messages(update.effective_chat.id)
    if lang == "de":
        text = f"🧹 Chat-Verlauf gelöscht ({n} Nachrichten). Ich starte gedächtnislos neu."
    else:
        text = f"🧹 Chat history cleared ({n} messages). Starting fresh."
    await update.effective_message.reply_text(text)


async def cmd_errors(update: Update, ctx) -> None:
    """Show the last N captured errors from store/errors.log."""
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    from cascade.error_log import tail_errors
    import datetime
    s = settings()
    parts = (update.effective_message.text or "").split()
    try:
        n = max(1, min(20, int(parts[1]))) if len(parts) > 1 else 5
    except ValueError:
        n = 5
    entries = tail_errors(n)
    if not entries:
        msg = de_en("Keine Fehler im Log.", "No errors logged.", lang)
        await update.effective_message.reply_text(msg)
        return

    tz = s.cascade_timezone
    try:
        from zoneinfo import ZoneInfo
        zi = ZoneInfo(tz)
    except Exception:
        zi = None

    lines = [de_en("*Letzte Fehler:*", "*Recent errors:*", lang), ""]
    for e in reversed(entries):
        dt = datetime.datetime.fromtimestamp(e.get("ts", 0), tz=zi) if zi else datetime.datetime.fromtimestamp(e.get("ts", 0))
        ts = dt.strftime("%H:%M:%S")
        scope = e.get("scope", "?")
        err = (e.get("error") or "")[:160]
        ctx_str = ", ".join(f"{k}={v}" for k, v in (e.get("context") or {}).items())[:200]
        lines.append(f"`{ts}` *{scope}*\n  {err}\n  _ctx:_ {ctx_str}")
    out = "\n".join(lines)
    if len(out) > 3800:
        out = out[:3800] + "…"
    await update.effective_message.reply_text(out, parse_mode=ParseMode.MARKDOWN)


async def cmd_unknown(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    cmd = (update.effective_message.text or "").split(maxsplit=1)[0]
    if lang == "de":
        text = f"Unbekanntes Kommando: `{cmd}`. Mit /help siehst du alle verfügbaren."
    else:
        text = f"Unknown command: `{cmd}`. Use /help to list everything."
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
