"""General commands: /help /start /whoami /settings + the catch-all unknown."""

from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode

from cascade.config import settings
from cascade.i18n import t
from cascade.store import Store

from ..helpers import lang_for, owner_only


async def cmd_help(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    await update.effective_message.reply_text(
        t("help", lang=lang_for(update)), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_start(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    if lang == "de":
        text = (
            "👋 *Willkommen bei Claude-Cascade*\n\n"
            "Ich bin ein Multi-Agent Coding-Bot.\n"
            "• Schreib mir eine Aufgabe als Text/Voice/Foto+Caption — ich plane, baue, prüfe.\n"
            "• Schreib mir eine Frage — ich antworte ohne Cascade zu starten.\n"
            "• Mit `/help` siehst du alle Commands.\n\n"
            "Probier z.B.:\n"
            "  „Erstelle eine kleine CLI in /tmp/foo die `--version` ausgibt"
        )
    else:
        text = (
            "👋 *Welcome to Claude-Cascade*\n\n"
            "I'm a multi-agent coding bot.\n"
            "• Send me a task (text/voice/photo+caption) — I plan, build, review.\n"
            "• Send me a question — I'll reply without spinning up a cascade.\n"
            "• `/help` shows all commands.\n\n"
            "Try e.g.:\n"
            "  \"Create a small CLI in /tmp/foo that prints --version\""
        )
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
    plan_e = sess.get("planner_effort") or s.cascade_planner_effort or "default"
    rev_e = sess.get("reviewer_effort") or s.cascade_reviewer_effort or "default"
    tri_e = sess.get("triage_effort") or s.cascade_triage_effort or "default"
    replan = sess.get("replan_max")
    replan_display = replan if replan is not None else f"default ({s.cascade_replan_max})"
    triage = "on" if s.cascade_triage_enabled else "off"
    auto_skill = "on" if s.cascade_auto_skill_suggest else "off"

    if lang == "de":
        text = (
            "⚙️ *Aktuelle Chat-Einstellungen*\n\n"
            f"*Repo:* `{repo}`\n"
            f"*Sprache:* `{lang}`\n\n"
            "*Modelle:*\n"
            f"• 🧠 Planner: `{plan_m}` (effort `{plan_e}`)\n"
            f"• 🛠 Implementer: `{impl_m}`\n"
            f"• 🔍 Reviewer: `{rev_m}` (effort `{rev_e}`)\n"
            f"• 🤖 Triage: effort `{tri_e}`, status `{triage}`\n\n"
            f"*Replan-Budget:* `{replan_display}`\n"
            f"*Auto-Skill-Vorschläge:* `{auto_skill}`\n\n"
            "Ändern via /repo /lang /models /effort /replan."
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
            f"• 🤖 Triage: effort `{tri_e}`, status `{triage}`\n\n"
            f"*Replan budget:* `{replan_display}`\n"
            f"*Auto-skill-suggestions:* `{auto_skill}`\n\n"
            "Change via /repo /lang /models /effort /replan."
        )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


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
