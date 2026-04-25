"""Telegram bot interface for Claude-Cascade.

Usage:
  python bot.py

Required env (.env):
  TELEGRAM_BOT_TOKEN   — from @BotFather
  TELEGRAM_OWNER_ID    — numeric Telegram user id of the sole authorized user
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from cascade.config import settings
from cascade.core import maintenance, run_cascade
from cascade.i18n import t
from cascade.models import (
    IMPLEMENTER_MODELS,
    PLANNER_REVIEWER_MODELS,
    implementer_display,
    implementer_provider,
)
from cascade.store import Store
from cascade.triage import triage

log = logging.getLogger("cascade.bot")

# Per-chat in-flight task registry: chat_id → (task_id, asyncio.Task, cancel_event)
_INFLIGHT: dict[int, tuple[str, asyncio.Task, asyncio.Event]] = {}

# Per-chat language override (chat_id → "de"|"en"); falls back to settings.cascade_bot_lang.
_LANG_OVERRIDE: dict[int, str] = {}

GIT_WHITELIST = {"status", "log", "diff", "branch", "checkout", "pull", "push", "commit", "show"}

EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]
REPLAN_CHOICES = [0, 1, 2, 3, 5]

# Pending skill suggestion per chat: chat_id → {task_id, suggestion_dict}
_PENDING_SKILL: dict[int, dict] = {}


def _lang(update: Update) -> str:
    if update.effective_chat and update.effective_chat.id in _LANG_OVERRIDE:
        return _LANG_OVERRIDE[update.effective_chat.id]
    return settings().cascade_bot_lang


def _local_tz():
    try:
        return ZoneInfo(settings().cascade_timezone)
    except ZoneInfoNotFoundError:
        return None


def _fmt_local(ts: float, fmt: str = "%H:%M:%S") -> str:
    tz = _local_tz()
    return datetime.fromtimestamp(ts, tz=tz).strftime(fmt)


# ------------------------------------------------------------------ helpers


def _is_owner(update: Update) -> bool:
    s = settings()
    user = update.effective_user
    return bool(user and s.telegram_owner_id and user.id == s.telegram_owner_id)


async def _owner_only(update: Update, _ctx) -> bool:
    if not _is_owner(update):
        log.warning("ignored unauthorized user id=%s", getattr(update.effective_user, "id", "?"))
        return False
    return True


async def _send(message: Message, text: str, *, code: bool = False) -> Message:
    if code:
        text = f"```\n{text[:3900]}\n```"
        return await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    return await message.reply_text(text[:4000])


def _fmt_status_emoji(status: str) -> str:
    return {
        "pending": "⏳",
        "running": "🔁",
        "interrupted": "⏸",
        "done": "✅",
        "failed": "❌",
        "cancelled": "🚫",
    }.get(status, "•")


# ------------------------------------------------------------------ task runner


async def _run_task_for_chat(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    task_text: str,
    *,
    attachments: list[Path] | None = None,
    resume_task_id: str | None = None,
) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    s = settings()

    store: Store = ctx.application.bot_data["store"]
    sess = await store.get_chat_session(chat.id) if chat else None
    repo = Path(sess["repo_path"]) if sess and sess.get("repo_path") else None
    impl_model = (sess or {}).get("implementer_model")
    impl_provider = implementer_provider(impl_model) if impl_model else None
    plan_model = (sess or {}).get("planner_model")
    rev_model = (sess or {}).get("reviewer_model")
    plan_effort = (sess or {}).get("planner_effort") or None
    rev_effort = (sess or {}).get("reviewer_effort") or None
    tri_effort = (sess or {}).get("triage_effort") or None
    chat_replan_max = (sess or {}).get("replan_max")

    lang = _lang(update)
    initial = t("progress.planning_initial", lang=lang)
    status_msg = await msg.reply_text(initial)
    cancel = asyncio.Event()

    state = {"last_text": initial, "skill_suggestion": None}

    async def progress(task_id: str, event: str, payload: dict) -> None:
        # Capture skill suggestions so we can prompt the user after the run.
        if event == "skill_suggested":
            state["skill_suggestion"] = {"task_id": task_id, **payload}
            return
        line = _format_progress_line(event, payload, lang)
        if not line:
            return
        new_text = state["last_text"] + "\n" + line
        if len(new_text) > 3500:
            new_text = "…\n" + new_text[-3400:]
        state["last_text"] = new_text
        try:
            await status_msg.edit_text(new_text)
        except Exception:
            pass

    coro = run_cascade(
        task=task_text,
        source="telegram",
        repo=repo,
        attachments=attachments,
        implementer_model=impl_model,
        implementer_provider=impl_provider,
        planner_model=plan_model,
        reviewer_model=rev_model,
        planner_effort=plan_effort,
        reviewer_effort=rev_effort,
        triage_effort=tri_effort,
        replan_max=chat_replan_max,
        progress=progress,
        s=s,
        store=store,
        cancel_event=cancel,
        resume_task_id=resume_task_id,
    )
    task_obj = asyncio.create_task(coro)

    # Stash cancel handle once we know the task_id (after first store write). We poll briefly.
    async def register_when_known() -> None:
        for _ in range(30):  # up to ~3s
            await asyncio.sleep(0.1)
            latest = await store.latest_task()
            if latest and latest.task_text == task_text:
                _INFLIGHT[chat.id] = (latest.id, task_obj, cancel)
                if chat:
                    await store.set_chat_last_task(chat.id, latest.id)
                return

    asyncio.create_task(register_when_known())

    try:
        await ctx.bot.send_chat_action(chat.id, ChatAction.TYPING)
        result = await task_obj
    except asyncio.CancelledError:
        await msg.reply_text(t("result.cancelled", lang=lang))
        return
    except Exception as e:
        await msg.reply_text(t("result.crashed", lang=lang, error=str(e)))
        return
    finally:
        _INFLIGHT.pop(chat.id, None)

    # Rich final report: status header + plan summary + changed files + diff excerpt
    header = t(
        "result.summary",
        lang=lang,
        emoji=_fmt_status_emoji(result.status),
        status=result.status,
        task_id=result.task_id,
        iterations=result.iterations,
        workspace=result.workspace_path,
        summary=result.summary,
    )
    parts = [header]
    if result.plan and result.plan.summary:
        label = "*Plan:*" if lang == "de" else "*Plan:*"
        parts.append(f"\n{label} {result.plan.summary[:400]}")
    if result.changed_files:
        label = "*Geänderte Dateien:*" if lang == "de" else "*Changed files:*"
        files_block = "\n".join(f"  • `{f}`" for f in result.changed_files[:15])
        more = (
            f"\n  … +{len(result.changed_files) - 15} weitere"
            if len(result.changed_files) > 15
            else ""
        )
        parts.append(f"\n{label}\n{files_block}{more}")
    full_msg = "\n".join(parts)
    if len(full_msg) > 3800:
        full_msg = full_msg[:3800] + "…"
    await msg.reply_text(full_msg, parse_mode=ParseMode.MARKDOWN)

    # Diff in a separate code-block message so it doesn't break parsing.
    if result.diff and result.diff.strip():
        diff_excerpt = result.diff[:3500]
        await msg.reply_text(
            f"```diff\n{diff_excerpt}\n```",
            parse_mode=ParseMode.MARKDOWN,
        )

    # Surface auto-skill-suggestion (if any) with inline accept/reject buttons.
    if result.status == "done" and state.get("skill_suggestion"):
        sug = state["skill_suggestion"]
        from cascade.skill_suggester import SkillSuggestion, format_skill_proposal
        try:
            sug_obj = SkillSuggestion.model_validate({k: v for k, v in sug.items() if k != "task_id"} | {"should_create": True})
            text = format_skill_proposal(sug_obj, lang=lang)
        except Exception:
            text = f"💡 Skill-Vorschlag: `{sug.get('name')}`"
        _PENDING_SKILL[chat.id] = sug
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("💾 " + ("Speichern" if lang == "de" else "Save"),
                                 callback_data=f"sk:y:{sug['name']}"),
            InlineKeyboardButton("❌ " + ("Verwerfen" if lang == "de" else "Discard"),
                                 callback_data=f"sk:n:{sug['name']}"),
        ]])
        await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


def _format_progress_line(event: str, payload: dict, lang: str = "de") -> str | None:
    if event == "started":
        return ""
    if event == "planning":
        return t("progress.planning", lang=lang)
    if event == "planned":
        return t("progress.planned", lang=lang, summary=(payload.get("summary") or "")[:120])
    if event == "implementing":
        return t("progress.implementing", lang=lang, n=payload.get("iteration"))
    if event == "implemented":
        return t(
            "progress.implemented",
            lang=lang,
            n=payload.get("iteration"),
            ops=payload.get("ops", 0),
            failed=payload.get("failed", 0),
        )
    if event == "reviewing":
        return t("progress.reviewing", lang=lang, n=payload.get("iteration"))
    if event == "reviewed":
        fb = (payload.get("feedback") or "").strip().splitlines()[0:1]
        suffix = f": {fb[0][:120]}" if fb else ""
        key = "progress.reviewed_pass" if payload.get("pass") else "progress.reviewed_fail"
        return t(key, lang=lang, n=payload.get("iteration"), suffix=suffix)
    if event in ("iteration_failed", "done"):
        return ""
    if event == "failed":
        return t("progress.failed", lang=lang, summary=payload.get("summary", ""))
    return None


# ------------------------------------------------------------------ handlers


async def cmd_help(update: Update, _ctx) -> None:
    if not await _owner_only(update, _ctx):
        return
    await update.effective_message.reply_text(t("help", lang=_lang(update)), parse_mode=ParseMode.MARKDOWN)


async def cmd_models(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    store: Store = ctx.application.bot_data["store"]
    sess = await store.get_chat_session(update.effective_chat.id) or {}
    s = settings()

    cur_plan = sess.get("planner_model") or s.cascade_planner_model
    cur_impl = sess.get("implementer_model") or s.cascade_implementer_model
    cur_rev = sess.get("reviewer_model") or s.cascade_reviewer_model

    text, kb = _models_main_view(lang, cur_plan, cur_impl, cur_rev)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


def _models_main_view(lang: str, cur_plan: str, cur_impl: str, cur_rev: str):
    if lang == "de":
        text = (
            "*Aktuelle Modell-Auswahl:*\n"
            f"• Planner:     `{cur_plan}`\n"
            f"• Implementer: `{cur_impl}`\n"
            f"• Reviewer:    `{cur_rev}`\n\n"
            "Welchen Worker willst du ändern?"
        )
        close = "✖ Schliessen"
    else:
        text = (
            "*Current model selection:*\n"
            f"• Planner:     `{cur_plan}`\n"
            f"• Implementer: `{cur_impl}`\n"
            f"• Reviewer:    `{cur_rev}`\n\n"
            "Which worker do you want to change?"
        )
        close = "✖ Close"
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🧠 Planner", callback_data="m:w:planner")],
            [InlineKeyboardButton("🛠 Implementer", callback_data="m:w:implementer")],
            [InlineKeyboardButton("🔍 Reviewer", callback_data="m:w:reviewer")],
            [InlineKeyboardButton(close, callback_data="m:close")],
        ]
    )
    return text, kb


async def on_models_callback(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    lang = _lang(update)
    store: Store = ctx.application.bot_data["store"]
    chat_id = update.effective_chat.id

    if data == "m:back":
        sess = await store.get_chat_session(chat_id) or {}
        s = settings()
        text, kb = _models_main_view(
            lang,
            sess.get("planner_model") or s.cascade_planner_model,
            sess.get("implementer_model") or s.cascade_implementer_model,
            sess.get("reviewer_model") or s.cascade_reviewer_model,
        )
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    if data == "m:close":
        await q.edit_message_text("✓" if lang == "en" else "✓ Geschlossen.")
        return

    if data.startswith("m:w:"):
        worker = data.split(":", 2)[2]
        if worker == "implementer":
            buttons = [
                [InlineKeyboardButton(display, callback_data=f"m:s:{worker}:{tag}")]
                for tag, (display, _prov) in IMPLEMENTER_MODELS.items()
            ]
        else:
            buttons = [
                [InlineKeyboardButton(display, callback_data=f"m:s:{worker}:{tag}")]
                for tag, display in PLANNER_REVIEWER_MODELS.items()
            ]
        back_label = "← Zurück" if lang == "de" else "← Back"
        buttons.append([InlineKeyboardButton(back_label, callback_data="m:back")])
        prompt = (
            f"Modell für *{worker}* wählen:" if lang == "de" else f"Pick model for *{worker}*:"
        )
        await q.edit_message_text(prompt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("m:s:"):
        _, _, worker, tag = data.split(":", 3)
        await store.set_chat_model(chat_id, worker, tag)
        # After selection: confirm + offer "back to main" so user can keep tweaking.
        sess = await store.get_chat_session(chat_id) or {}
        s = settings()
        text, kb = _models_main_view(
            lang,
            sess.get("planner_model") or s.cascade_planner_model,
            sess.get("implementer_model") or s.cascade_implementer_model,
            sess.get("reviewer_model") or s.cascade_reviewer_model,
        )
        confirm = (
            f"✅ {worker} → `{tag}`\n\n{text}"
            if lang == "de"
            else f"✅ {worker} → `{tag}`\n\n{text}"
        )
        await q.edit_message_text(confirm, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return


async def cmd_effort(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    store: Store = ctx.application.bot_data["store"]
    sess = await store.get_chat_session(update.effective_chat.id) or {}
    s = settings()

    cur_p = sess.get("planner_effort") or s.cascade_planner_effort or "default"
    cur_r = sess.get("reviewer_effort") or s.cascade_reviewer_effort or "default"
    cur_t = sess.get("triage_effort") or s.cascade_triage_effort or "default"

    text, kb = _effort_main_view(lang, cur_p, cur_r, cur_t)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


def _effort_main_view(lang: str, p: str, r: str, t: str):
    if lang == "de":
        text = (
            "*Aktuelle Effort-Stufen:*\n"
            f"• Planner:  `{p}`\n"
            f"• Reviewer: `{r}`\n"
            f"• Triage:   `{t}`\n\n"
            "Welchen Worker ändern?"
        )
        close = "✖ Schliessen"
    else:
        text = (
            "*Current effort levels:*\n"
            f"• Planner:  `{p}`\n"
            f"• Reviewer: `{r}`\n"
            f"• Triage:   `{t}`\n\n"
            "Which worker do you want to change?"
        )
        close = "✖ Close"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 Planner", callback_data="e:w:planner")],
        [InlineKeyboardButton("🔍 Reviewer", callback_data="e:w:reviewer")],
        [InlineKeyboardButton("🤖 Triage", callback_data="e:w:triage")],
        [InlineKeyboardButton(close, callback_data="e:close")],
    ])
    return text, kb


async def on_effort_callback(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    lang = _lang(update)
    store: Store = ctx.application.bot_data["store"]
    chat_id = update.effective_chat.id

    if data == "e:back":
        sess = await store.get_chat_session(chat_id) or {}
        s = settings()
        text, kb = _effort_main_view(
            lang,
            sess.get("planner_effort") or s.cascade_planner_effort or "default",
            sess.get("reviewer_effort") or s.cascade_reviewer_effort or "default",
            sess.get("triage_effort") or s.cascade_triage_effort or "default",
        )
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    if data == "e:close":
        await q.edit_message_text("✓" if lang == "en" else "✓ Geschlossen.")
        return

    if data.startswith("e:w:"):
        worker = data.split(":", 2)[2]
        buttons = [
            [InlineKeyboardButton(level, callback_data=f"e:s:{worker}:{level}")]
            for level in EFFORT_LEVELS
        ]
        # Allow clearing back to default
        buttons.append([InlineKeyboardButton(
            "⟲ default" if lang == "en" else "⟲ Standard",
            callback_data=f"e:s:{worker}:_clear")])
        buttons.append([InlineKeyboardButton(
            "← Back" if lang == "en" else "← Zurück", callback_data="e:back")])
        prompt = f"Effort für *{worker}* wählen:" if lang == "de" else f"Pick effort for *{worker}*:"
        await q.edit_message_text(prompt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("e:s:"):
        _, _, worker, level = data.split(":", 3)
        value = None if level == "_clear" else level
        await store.set_chat_effort(chat_id, worker, value)
        sess = await store.get_chat_session(chat_id) or {}
        s = settings()
        text, kb = _effort_main_view(
            lang,
            sess.get("planner_effort") or s.cascade_planner_effort or "default",
            sess.get("reviewer_effort") or s.cascade_reviewer_effort or "default",
            sess.get("triage_effort") or s.cascade_triage_effort or "default",
        )
        shown = value or ("default" if lang == "en" else "Standard")
        confirm = f"✅ {worker} → `{shown}`\n\n{text}"
        await q.edit_message_text(confirm, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return


async def cmd_replan(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    store: Store = ctx.application.bot_data["store"]
    sess = await store.get_chat_session(update.effective_chat.id) or {}
    s = settings()
    cur = sess.get("replan_max")
    cur_display = cur if cur is not None else f"default ({s.cascade_replan_max})"

    args = ctx.args or []
    if not args:
        # Show current + inline keyboard with quick choices
        if lang == "de":
            head = (
                f"*Replan-Budget* — Anzahl Replans wenn Loop steckenbleibt.\n"
                f"Aktuell: `{cur_display}`\n\n"
                f"Wähle eine Stufe oder nutze `/replan <n>`:"
            )
        else:
            head = (
                f"*Replan budget* — how often the planner can rewrite the plan when the loop is stuck.\n"
                f"Current: `{cur_display}`\n\n"
                f"Pick a level or use `/replan <n>`:"
            )
        buttons = [
            [InlineKeyboardButton(f"{n} — {'aus' if (lang=='de' and n==0) else ('off' if n==0 else f'{n}×')}",
                                   callback_data=f"r:s:{n}")]
            for n in REPLAN_CHOICES
        ]
        buttons.append([InlineKeyboardButton(
            "⟲ Standard" if lang == "de" else "⟲ default", callback_data="r:s:_clear")])
        await update.effective_message.reply_text(
            head, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    try:
        n = int(args[0])
        if n < 0 or n > 10:
            raise ValueError("out of range 0..10")
    except ValueError:
        await update.effective_message.reply_text(
            "Aufruf: /replan <n>  (n=0..10)" if lang == "de" else "Usage: /replan <n>  (n=0..10)"
        )
        return
    await store.set_chat_replan_max(update.effective_chat.id, n)
    await update.effective_message.reply_text(
        f"✅ Replan-Budget = `{n}`" if lang == "de" else f"✅ Replan budget = `{n}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def on_replan_callback(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    q = update.callback_query
    await q.answer()
    lang = _lang(update)
    data = q.data or ""
    store: Store = ctx.application.bot_data["store"]
    chat_id = update.effective_chat.id
    if data.startswith("r:s:"):
        raw = data.split(":", 2)[2]
        if raw == "_clear":
            await store.set_chat_replan_max(chat_id, None)
            txt = "✅ Replan-Budget = Standard" if lang == "de" else "✅ Replan budget = default"
        else:
            n = int(raw)
            await store.set_chat_replan_max(chat_id, n)
            txt = f"✅ Replan-Budget = `{n}`" if lang == "de" else f"✅ Replan budget = `{n}`"
        await q.edit_message_text(txt, parse_mode=ParseMode.MARKDOWN)


async def cmd_skills(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    store: Store = ctx.application.bot_data["store"]
    args = ctx.args or []
    if args and args[0] == "delete" and len(args) >= 2:
        ok = await store.delete_skill(args[1])
        await update.effective_message.reply_text(
            ("✅ Gelöscht." if ok else "Skill nicht gefunden.") if lang == "de"
            else ("✅ Deleted." if ok else "Skill not found.")
        )
        return

    skills = await store.list_skills()
    if not skills:
        await update.effective_message.reply_text(
            "Keine Skills gespeichert. Sie entstehen automatisch nach erfolgreichen Runs."
            if lang == "de" else
            "No skills saved yet. They are auto-suggested after successful runs."
        )
        return
    lines = ["*Gespeicherte Skills:*" if lang == "de" else "*Saved skills:*"]
    for sk in skills:
        used = sk.get("usage_count", 0)
        lines.append(f"• `{sk['name']}` — {sk.get('description') or '—'} (× {used})")
    lines.append("")
    lines.append(
        "Aufruf: `/run <name> <args>` — Platzhalter werden mit den args ersetzt."
        if lang == "de" else
        "Usage: `/run <name> <args>` — placeholders are replaced with args."
    )
    lines.append(
        "Löschen: `/skills delete <name>`" if lang == "de" else
        "Delete: `/skills delete <name>`"
    )
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_run_skill(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    store: Store = ctx.application.bot_data["store"]
    args = ctx.args or []
    if not args:
        await update.effective_message.reply_text(
            "Aufruf: /run <skill_name> <args …>" if lang == "de"
            else "Usage: /run <skill_name> <args …>"
        )
        return
    name = args[0]
    sk = await store.get_skill_by_name(name)
    if not sk:
        await update.effective_message.reply_text(
            f"Skill `{name}` nicht gefunden. /skills für Liste."
            if lang == "de" else f"Skill `{name}` not found. /skills for list.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    template = sk["task_template"]
    params = args[1:]
    # Two strategies: positional {0}/{1}/... AND key=value pairs.
    text = template
    kv = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in params if "=" in p}
    rest = [p for p in params if "=" not in p]
    try:
        text = template.format(*rest, **kv)
    except (KeyError, IndexError):
        # If formatting fails, fall back to template + free-form args appended.
        text = template + ("\n\n" + " ".join(params) if params else "")
    await store.increment_skill_usage(name)
    await _run_task_for_chat(update, ctx, text)


async def on_skill_callback(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    lang = _lang(update)
    store: Store = ctx.application.bot_data["store"]
    chat_id = update.effective_chat.id

    if data.startswith("sk:y:"):
        name = data.split(":", 2)[2]
        sug = _PENDING_SKILL.pop(chat_id, None)
        if not sug or sug.get("name") != name:
            await q.edit_message_text("⚠ Vorschlag nicht mehr verfügbar." if lang == "de" else "⚠ Suggestion no longer available.")
            return
        try:
            await store.create_skill(
                name=sug["name"],
                description=sug.get("description"),
                task_template=sug["task_template"],
                rationale=sug.get("rationale"),
                source_task_ids=[sug.get("task_id")] if sug.get("task_id") else [],
            )
            if sug.get("task_id"):
                await store.mark_skill_suggestion_decided(sug["task_id"], "accepted")
            await q.edit_message_text(
                f"✅ Skill `{name}` gespeichert. Aufruf via `/run {name} <args>`."
                if lang == "de" else
                f"✅ Skill `{name}` saved. Use `/run {name} <args>`.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            await q.edit_message_text(f"❌ {e}")
        return
    if data.startswith("sk:n:"):
        sug = _PENDING_SKILL.pop(chat_id, None)
        if sug and sug.get("task_id"):
            await store.mark_skill_suggestion_decided(sug["task_id"], "rejected")
        await q.edit_message_text("Verworfen." if lang == "de" else "Discarded.")
        return


async def cmd_lang(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    args = ctx.args or []
    chat_id = update.effective_chat.id
    current = _LANG_OVERRIDE.get(chat_id, settings().cascade_bot_lang)
    if not args:
        await update.effective_message.reply_text(
            t("lang.usage", lang=current, current=current), parse_mode=ParseMode.MARKDOWN
        )
        return
    new = args[0].lower()
    if new not in ("de", "en"):
        await update.effective_message.reply_text(t("lang.usage", lang=current, current=current), parse_mode=ParseMode.MARKDOWN)
        return
    _LANG_OVERRIDE[chat_id] = new
    # template uses {lang} which collides with our `lang` kwarg — pre-format manually.
    msg_template = "Sprache auf `{}` umgestellt." if new == "de" else "Language switched to `{}`."
    await update.effective_message.reply_text(msg_template.format(new), parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    store: Store = ctx.application.bot_data["store"]
    args = ctx.args or []
    if args:
        task = await store.get_task(args[0])
    else:
        task = await store.latest_task()
    if not task:
        await update.effective_message.reply_text(t("no_tasks", lang=lang))
        return
    await update.effective_message.reply_text(
        t(
            "status_line",
            lang=lang,
            emoji=_fmt_status_emoji(task.status),
            status=task.status,
            task_id=task.id,
            task=task.task_text[:200],
            iteration=task.iteration,
            summary=task.result_summary or "—",
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_logs(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    store: Store = ctx.application.bot_data["store"]
    args = ctx.args or []
    if args:
        tid = args[0]
    else:
        latest = await store.latest_task()
        if not latest:
            await update.effective_message.reply_text(t("no_tasks", lang=lang))
            return
        tid = latest.id
    entries = await store.tail_logs(tid, n=50)
    if not entries:
        await update.effective_message.reply_text(t("no_logs", lang=lang))
        return
    text = "\n".join(
        f"{_fmt_local(e.ts)} [{e.level}] {e.message}" for e in entries
    )
    await _send(update.effective_message, text, code=True)


async def cmd_cancel(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    args = ctx.args or []
    target_id: str | None = args[0] if args else None
    if target_id is None:
        chat_id = update.effective_chat.id
        if chat_id not in _INFLIGHT:
            await update.effective_message.reply_text(t("no_inflight", lang=lang))
            return
        target_id, _, ev = _INFLIGHT[chat_id]
        ev.set()
        await update.effective_message.reply_text(
            t("cancel_sent", lang=lang, task_id=target_id), parse_mode=ParseMode.MARKDOWN
        )
        return
    for cid, (tid, _task, ev) in list(_INFLIGHT.items()):
        if tid == target_id:
            ev.set()
            await update.effective_message.reply_text(
                t("cancel_sent", lang=lang, task_id=target_id), parse_mode=ParseMode.MARKDOWN
            )
            return
    await update.effective_message.reply_text(
        t("cancel_not_running", lang=lang, task_id=target_id), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_history(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    store: Store = ctx.application.bot_data["store"]
    tasks = await store.list_tasks(limit=10)
    if not tasks:
        await update.effective_message.reply_text(t("no_tasks", lang=lang))
        return
    lines = [
        f"{_fmt_status_emoji(task.status)} `{task.id}` i={task.iteration} {task.task_text[:80]}"
        for task in tasks
    ]
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_repo(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    store: Store = ctx.application.bot_data["store"]
    chat_id = update.effective_chat.id
    args = ctx.args or []
    if not args:
        sess = await store.get_chat_session(chat_id)
        path = sess.get("repo_path") if sess else None
        await update.effective_message.reply_text(
            t("repo.current", lang=lang, path=path or "—"), parse_mode=ParseMode.MARKDOWN
        )
        return
    if args[0].lower() in ("clear", "none", "off"):
        await store.set_chat_repo(chat_id, None)
        await update.effective_message.reply_text(t("repo.cleared", lang=lang))
        return
    p = Path(args[0]).expanduser().resolve()
    if not p.exists():
        await update.effective_message.reply_text(t("repo.not_found", lang=lang, path=p), parse_mode=ParseMode.MARKDOWN)
        return
    await store.set_chat_repo(chat_id, str(p))
    await update.effective_message.reply_text(t("repo.set", lang=lang, path=p), parse_mode=ParseMode.MARKDOWN)


async def cmd_resume(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    args = ctx.args or []
    if not args:
        await update.effective_message.reply_text(t("resume.usage", lang=lang))
        return
    store: Store = ctx.application.bot_data["store"]
    task = await store.get_task(args[0])
    if not task:
        await update.effective_message.reply_text(
            t("task_not_found", lang=lang, task_id=args[0]), parse_mode=ParseMode.MARKDOWN
        )
        return
    await _run_task_for_chat(update, ctx, task.task_text, resume_task_id=task.id)


async def cmd_exec(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    if not ctx.args:
        await update.effective_message.reply_text(t("exec.usage", lang=lang))
        return
    cmd = " ".join(ctx.args)
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            await _send(update.effective_message, t("exec.timeout", lang=lang), code=True)
            return
        out = out_b.decode("utf-8", errors="replace")
        suffix = f"\n[exit {proc.returncode}]"
        await _send(update.effective_message, (out or t("exec.no_output", lang=lang)) + suffix, code=True)
    except Exception as e:
        await _send(update.effective_message, f"error: {e}", code=True)


async def cmd_git(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    if not ctx.args or len(ctx.args) < 2:
        await update.effective_message.reply_text(t("git.usage", lang=lang))
        return
    repo = Path(ctx.args[0]).expanduser().resolve()
    sub = ctx.args[1]
    if sub not in GIT_WHITELIST:
        await update.effective_message.reply_text(
            t("git.not_whitelisted", lang=lang, sub=sub, whitelist=sorted(GIT_WHITELIST)),
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if not (repo / ".git").exists():
        await update.effective_message.reply_text(
            t("git.not_a_repo", lang=lang, path=repo), parse_mode=ParseMode.MARKDOWN
        )
        return
    rest = ctx.args[2:]
    cmd = ["git", "-C", str(repo), sub, *rest]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        out = out_b.decode("utf-8", errors="replace")
        await _send(update.effective_message, (out or "(no output)") + f"\n[exit {proc.returncode}]", code=True)
    except Exception as e:
        await _send(update.effective_message, f"error: {e}", code=True)


# ----- message handlers -----


async def on_text(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    text = (update.effective_message.text or "").strip()
    if not text:
        return
    lang = _lang(update)
    s = settings()

    # Build short context of last 3 tasks so the conversation layer can answer
    # follow-up questions like "was hast du gemacht?" or "wo liegt das?".
    store: Store = ctx.application.bot_data["store"]
    recent = await store.list_tasks(limit=3)
    context_lines = []
    for t in recent:
        files_hint = ""
        try:
            iters = await store.list_iterations(t.id)
            last = iters[-1] if iters else None
            if last and last.diff_excerpt:
                # Pull file names from "diff --git a/X b/X" lines
                import re
                files = sorted({m.group(1) for m in re.finditer(r"diff --git a/(\S+) ", last.diff_excerpt or "")})
                if files:
                    files_hint = f" files=[{', '.join(files[:6])}]"
        except Exception:
            pass
        context_lines.append(
            f"- task_id={t.id} status={t.status} iter={t.iteration} "
            f"workspace={t.workspace_path or '—'} summary={(t.result_summary or '—')[:120]} "
            f"task={(t.task_text or '')[:140]}{files_hint}"
        )
    context = "\n".join(context_lines) if context_lines else None

    # Per-chat triage_effort override (so /effort triage low/high actually applies
    # to the on_text triage call too, not only to in-cascade triage).
    sess_now = await store.get_chat_session(update.effective_chat.id) or {}
    if sess_now.get("triage_effort"):
        s = s.model_copy(update={"cascade_triage_effort": sess_now["triage_effort"]})

    try:
        verdict = await triage(text, lang=lang, s=s, context=context)
    except Exception as e:
        log.warning("triage crashed (%s) — treating as task", e)
        await _run_task_for_chat(update, ctx, text)
        return

    if verdict.is_task:
        await _run_task_for_chat(update, ctx, verdict.task or text)
    else:
        reply = verdict.reply or ("Ok." if lang == "de" else "Ok.")
        await update.effective_message.reply_text(reply)


async def on_voice(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    s = settings()
    if not s.openai_api_key:
        await update.effective_message.reply_text(t("voice.no_key", lang=lang))
        return
    msg = update.effective_message
    voice = msg.voice or msg.audio
    file = await ctx.bot.get_file(voice.file_id)
    target = s.workspaces_dir / "_voice" / f"{voice.file_unique_id}.ogg"
    target.parent.mkdir(parents=True, exist_ok=True)
    await file.download_to_drive(str(target))

    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=s.openai_api_key)
    with open(target, "rb") as f:
        transcript = await client.audio.transcriptions.create(model="whisper-1", file=f)
    text = (transcript.text or "").strip()
    if not text:
        await msg.reply_text(t("voice.empty", lang=lang))
        return
    await msg.reply_text(t("voice.transcript", lang=lang, text=text[:300]), parse_mode=ParseMode.MARKDOWN)
    await _run_task_for_chat(update, ctx, text)


async def on_photo_or_document(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    s = settings()
    msg = update.effective_message
    caption = (msg.caption or "").strip()
    if not caption:
        await msg.reply_text(t("photo.no_caption", lang=lang))
        return

    attachments: list[Path] = []
    if msg.photo:
        photo = msg.photo[-1]  # largest
        f = await ctx.bot.get_file(photo.file_id)
        target = s.workspaces_dir / "_attachments" / f"{photo.file_unique_id}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        await f.download_to_drive(str(target))
        attachments.append(target)
    if msg.document:
        doc = msg.document
        f = await ctx.bot.get_file(doc.file_id)
        target = s.workspaces_dir / "_attachments" / (doc.file_name or f"{doc.file_unique_id}.bin")
        target.parent.mkdir(parents=True, exist_ok=True)
        await f.download_to_drive(str(target))
        attachments.append(target)

    await _run_task_for_chat(update, ctx, caption, attachments=attachments)


# ------------------------------------------------------------------ startup


async def post_init(application: Application) -> None:
    s = settings()
    store = await Store.open(s.cascade_db_path)
    application.bot_data["store"] = store

    # Auto-resume: any 'running' tasks must be from a previous bot life — mark and continue.
    interrupted = await store.mark_running_as_interrupted()
    if interrupted and s.telegram_owner_id:
        try:
            await application.bot.send_message(
                chat_id=s.telegram_owner_id,
                text=t(
                    "startup.interrupted",
                    lang=s.cascade_bot_lang,
                    n=len(interrupted),
                    ids=", ".join(f"`{i}`" for i in interrupted),
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            log.warning("could not notify owner of interrupted tasks: %s", e)


async def post_shutdown(application: Application) -> None:
    store: Store | None = application.bot_data.get("store")
    if store is not None:
        await store.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    s = settings()
    if not s.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set in .env")
    if not s.telegram_owner_id:
        raise SystemExit("TELEGRAM_OWNER_ID not set in .env")

    app = (
        Application.builder()
        .token(s.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("repo", cmd_repo))
    app.add_handler(CommandHandler("exec", cmd_exec))
    app.add_handler(CommandHandler("git", cmd_git))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("models", cmd_models))
    app.add_handler(CommandHandler("effort", cmd_effort))
    app.add_handler(CommandHandler("replan", cmd_replan))
    app.add_handler(CommandHandler("skills", cmd_skills))
    app.add_handler(CommandHandler("run", cmd_run_skill))
    app.add_handler(CallbackQueryHandler(on_models_callback, pattern=r"^m:"))
    app.add_handler(CallbackQueryHandler(on_effort_callback, pattern=r"^e:"))
    app.add_handler(CallbackQueryHandler(on_replan_callback, pattern=r"^r:"))
    app.add_handler(CallbackQueryHandler(on_skill_callback, pattern=r"^sk:"))

    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, on_photo_or_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Cascade bot starting; owner=%s", s.telegram_owner_id)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
