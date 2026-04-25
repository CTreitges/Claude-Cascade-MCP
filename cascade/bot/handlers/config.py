"""Config commands: /repo /lang /models /effort /replan + their callbacks."""

from __future__ import annotations

from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode

from cascade.config import settings
from cascade.i18n import t
from cascade.models import IMPLEMENTER_MODELS, PLANNER_REVIEWER_MODELS
from cascade.store import Store

from ..helpers import lang_for, owner_only
from ..state import EFFORT_LEVELS, LANG_OVERRIDE, REPLAN_CHOICES


# ---------- /repo ----------

async def cmd_repo(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
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
        await update.effective_message.reply_text(
            t("repo.not_found", lang=lang, path=p), parse_mode=ParseMode.MARKDOWN
        )
        return
    await store.set_chat_repo(chat_id, str(p))
    await update.effective_message.reply_text(
        t("repo.set", lang=lang, path=p), parse_mode=ParseMode.MARKDOWN
    )


# ---------- /lang ----------

async def cmd_lang(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    args = ctx.args or []
    chat_id = update.effective_chat.id
    current = LANG_OVERRIDE.get(chat_id, settings().cascade_bot_lang)
    if not args:
        await update.effective_message.reply_text(
            t("lang.usage", lang=current, current=current), parse_mode=ParseMode.MARKDOWN
        )
        return
    new = args[0].lower()
    if new not in ("de", "en"):
        await update.effective_message.reply_text(
            t("lang.usage", lang=current, current=current), parse_mode=ParseMode.MARKDOWN
        )
        return
    LANG_OVERRIDE[chat_id] = new
    msg_template = "Sprache auf `{}` umgestellt." if new == "de" else "Language switched to `{}`."
    await update.effective_message.reply_text(
        msg_template.format(new), parse_mode=ParseMode.MARKDOWN
    )


# ---------- /models ----------

def models_main_view(lang: str, cur_plan: str, cur_impl: str, cur_rev: str):
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
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 Planner", callback_data="m:w:planner")],
        [InlineKeyboardButton("🛠 Implementer", callback_data="m:w:implementer")],
        [InlineKeyboardButton("🔍 Reviewer", callback_data="m:w:reviewer")],
        [InlineKeyboardButton(close, callback_data="m:close")],
    ])
    return text, kb


async def cmd_models(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    sess = await store.get_chat_session(update.effective_chat.id) or {}
    s = settings()
    text, kb = models_main_view(
        lang,
        sess.get("planner_model") or s.cascade_planner_model,
        sess.get("implementer_model") or s.cascade_implementer_model,
        sess.get("reviewer_model") or s.cascade_reviewer_model,
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def on_models_callback(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    chat_id = update.effective_chat.id

    if data == "m:back":
        sess = await store.get_chat_session(chat_id) or {}
        s = settings()
        text, kb = models_main_view(
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
        prompt = f"Modell für *{worker}* wählen:" if lang == "de" else f"Pick model for *{worker}*:"
        await q.edit_message_text(
            prompt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("m:s:"):
        _, _, worker, tag = data.split(":", 3)
        await store.set_chat_model(chat_id, worker, tag)
        sess = await store.get_chat_session(chat_id) or {}
        s = settings()
        text, kb = models_main_view(
            lang,
            sess.get("planner_model") or s.cascade_planner_model,
            sess.get("implementer_model") or s.cascade_implementer_model,
            sess.get("reviewer_model") or s.cascade_reviewer_model,
        )
        confirm = f"✅ {worker} → `{tag}`\n\n{text}"
        await q.edit_message_text(confirm, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return


# ---------- /effort ----------

def effort_main_view(lang: str, p: str, r: str, t_eff: str):
    if lang == "de":
        text = (
            "*Aktuelle Effort-Stufen:*\n"
            f"• Planner:  `{p}`\n"
            f"• Reviewer: `{r}`\n"
            f"• Triage:   `{t_eff}`\n\n"
            "Welchen Worker ändern?"
        )
        close = "✖ Schliessen"
    else:
        text = (
            "*Current effort levels:*\n"
            f"• Planner:  `{p}`\n"
            f"• Reviewer: `{r}`\n"
            f"• Triage:   `{t_eff}`\n\n"
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


async def cmd_effort(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    sess = await store.get_chat_session(update.effective_chat.id) or {}
    s = settings()
    text, kb = effort_main_view(
        lang,
        sess.get("planner_effort") or s.cascade_planner_effort or "default",
        sess.get("reviewer_effort") or s.cascade_reviewer_effort or "default",
        sess.get("triage_effort") or s.cascade_triage_effort or "default",
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def on_effort_callback(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    chat_id = update.effective_chat.id

    if data == "e:back":
        sess = await store.get_chat_session(chat_id) or {}
        s = settings()
        text, kb = effort_main_view(
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
        buttons.append([InlineKeyboardButton(
            "⟲ default" if lang == "en" else "⟲ Standard",
            callback_data=f"e:s:{worker}:_clear",
        )])
        buttons.append([InlineKeyboardButton(
            "← Back" if lang == "en" else "← Zurück", callback_data="e:back",
        )])
        prompt = f"Effort für *{worker}* wählen:" if lang == "de" else f"Pick effort for *{worker}*:"
        await q.edit_message_text(
            prompt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("e:s:"):
        _, _, worker, level = data.split(":", 3)
        value = None if level == "_clear" else level
        await store.set_chat_effort(chat_id, worker, value)
        sess = await store.get_chat_session(chat_id) or {}
        s = settings()
        text, kb = effort_main_view(
            lang,
            sess.get("planner_effort") or s.cascade_planner_effort or "default",
            sess.get("reviewer_effort") or s.cascade_reviewer_effort or "default",
            sess.get("triage_effort") or s.cascade_triage_effort or "default",
        )
        shown = value or ("default" if lang == "en" else "Standard")
        confirm = f"✅ {worker} → `{shown}`\n\n{text}"
        await q.edit_message_text(confirm, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return


# ---------- /replan ----------

async def cmd_replan(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    sess = await store.get_chat_session(update.effective_chat.id) or {}
    s = settings()
    cur = sess.get("replan_max")
    cur_display = cur if cur is not None else f"default ({s.cascade_replan_max})"

    args = ctx.args or []
    if not args:
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
            [InlineKeyboardButton(
                f"{n} — {'aus' if (lang == 'de' and n == 0) else ('off' if n == 0 else f'{n}×')}",
                callback_data=f"r:s:{n}",
            )]
            for n in REPLAN_CHOICES
        ]
        buttons.append([InlineKeyboardButton(
            "⟲ Standard" if lang == "de" else "⟲ default", callback_data="r:s:_clear",
        )])
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
    if not await owner_only(update, ctx):
        return
    q = update.callback_query
    await q.answer()
    lang = lang_for(update)
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
