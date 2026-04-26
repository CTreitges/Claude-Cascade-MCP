"""Config commands: /repo /lang /models /effort /replan + their callbacks."""

from __future__ import annotations

from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode

from cascade.config import settings
from cascade.i18n import t
from cascade.models import (
    CHAT_MODELS,
    IMPLEMENTER_MODELS,
    PLANNER_REVIEWER_MODELS,
    effort_levels_for,
    model_supports_effort,
)
from cascade.store import Store

from ..helpers import lang_for, owner_only
from ..state import ITERATION_CHOICES, LANG_OVERRIDE, REPLAN_CHOICES, UNLIMITED_SENTINEL


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
    # Persist so the choice survives bot restarts (helpers.lang_for reads
    # back from sessions.lang via lifecycle.post_init's warm-up).
    store: Store = ctx.application.bot_data["store"]
    try:
        await store.set_chat_lang(chat_id, new)
    except Exception:
        pass
    msg_template = "Sprache auf `{}` umgestellt." if new == "de" else "Language switched to `{}`."
    await update.effective_message.reply_text(
        msg_template.format(new), parse_mode=ParseMode.MARKDOWN
    )


# ---------- /models ----------

def models_main_view(lang: str, cur_plan: str, cur_impl: str, cur_rev: str, cur_chat: str):
    if lang == "de":
        text = (
            "*Aktuelle Modell-Auswahl:*\n"
            f"• Planner:     `{cur_plan}`\n"
            f"• Implementer: `{cur_impl}`\n"
            f"• Reviewer:    `{cur_rev}`\n"
            f"• Chat:        `{cur_chat}`\n\n"
            "Welchen Worker willst du ändern?"
        )
        close = "✖ Schliessen"
    else:
        text = (
            "*Current model selection:*\n"
            f"• Planner:     `{cur_plan}`\n"
            f"• Implementer: `{cur_impl}`\n"
            f"• Reviewer:    `{cur_rev}`\n"
            f"• Chat:        `{cur_chat}`\n\n"
            "Which worker do you want to change?"
        )
        close = "✖ Close"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 Planner", callback_data="m:w:planner")],
        [InlineKeyboardButton("🛠 Implementer", callback_data="m:w:implementer")],
        [InlineKeyboardButton("🔍 Reviewer", callback_data="m:w:reviewer")],
        [InlineKeyboardButton("💬 Chat", callback_data="m:w:chat")],
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
        sess.get("chat_model") or s.cascade_triage_model,
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
            sess.get("chat_model") or s.cascade_triage_model,
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
        elif worker == "chat":
            buttons = [
                [InlineKeyboardButton(display, callback_data=f"m:s:{worker}:{tag}")]
                for tag, display in CHAT_MODELS.items()
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
            sess.get("chat_model") or s.cascade_triage_model,
        )
        confirm = f"✅ {worker} → `{tag}`\n\n{text}"
        await q.edit_message_text(confirm, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return


# ---------- /effort ----------

def effort_main_view(lang: str, sess: dict | None, s):
    """Render the /effort root view dynamically.

    For each worker:
    - Claude model → effort knob (low/medium/high[/xhigh/max])
    - Ollama model → temperature knob (0.0/0.2/0.5/0.7/1.0)

    Both are shown side-by-side in the main view; the button leads to the
    matching submenu based on the model's provider.
    """
    sess = sess or {}
    # cb_key (used in callback_data and store column prefix), label,
    # model_tag, current_effort, current_temperature
    workers = [
        ("planner", "🧠 Planner",
         sess.get("planner_model") or s.cascade_planner_model,
         sess.get("planner_effort") or s.cascade_planner_effort or "default",
         sess.get("planner_temperature")),
        ("implementer", "🛠 Implementer",
         sess.get("implementer_model") or s.cascade_implementer_model,
         sess.get("implementer_effort") or s.cascade_implementer_effort or "default",
         sess.get("implementer_temperature")),
        ("reviewer", "🔍 Reviewer",
         sess.get("reviewer_model") or s.cascade_reviewer_model,
         sess.get("reviewer_effort") or s.cascade_reviewer_effort or "default",
         sess.get("reviewer_temperature")),
        ("triage", "💬 Chat",
         sess.get("chat_model") or s.cascade_triage_model,
         sess.get("triage_effort") or s.cascade_triage_effort or "default",
         sess.get("chat_temperature")),
    ]

    header = (
        "*Effort & Temperature pro Worker*\n"
        "_Claude-Modelle: Effort-Stufe. Ollama-Modelle: Temperature._"
        if lang == "de"
        else "*Effort & temperature per worker*\n"
             "_Claude models: effort level. Ollama models: temperature._"
    )

    lines = [header, ""]
    button_rows = []
    for cb, label, model, eff, temp in workers:
        if model_supports_effort(model):
            lines.append(f"• {label}: effort `{eff}` _({model})_")
        else:
            shown_temp = "default" if temp is None else f"{float(temp):.2f}"
            lines.append(f"• {label}: temperature `{shown_temp}` _({model})_")
        button_rows.append([
            InlineKeyboardButton(label, callback_data=f"e:w:{cb}")
        ])

    prompt = ("\nWelchen Worker ändern?" if lang == "de"
              else "\nWhich worker do you want to change?")
    lines.append(prompt)

    close = "✖ Schliessen" if lang == "de" else "✖ Close"
    button_rows.append([InlineKeyboardButton(close, callback_data="e:close")])
    return "\n".join(lines), InlineKeyboardMarkup(button_rows)


async def cmd_effort(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    sess = await store.get_chat_session(update.effective_chat.id) or {}
    s = settings()
    text, kb = effort_main_view(lang, sess, s)
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
        text, kb = effort_main_view(lang, sess, s)
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    if data == "e:close":
        await q.edit_message_text("✓" if lang == "en" else "✓ Geschlossen.")
        return

    if data.startswith("e:w:"):
        worker = data.split(":", 2)[2]
        worker_label = "Chat" if worker == "triage" else worker
        sess = await store.get_chat_session(chat_id) or {}
        s = settings()
        model_for_worker = {
            "planner":     sess.get("planner_model")     or s.cascade_planner_model,
            "implementer": sess.get("implementer_model") or s.cascade_implementer_model,
            "reviewer":    sess.get("reviewer_model")    or s.cascade_reviewer_model,
            "triage":      sess.get("chat_model")        or s.cascade_triage_model,
        }.get(worker)
        is_claude = (model_for_worker or "").startswith("claude-")

        if is_claude:
            levels = effort_levels_for(model_for_worker)
            buttons = [
                [InlineKeyboardButton(level, callback_data=f"e:s:{worker}:{level}")]
                for level in levels
            ]
            buttons.append([InlineKeyboardButton(
                "⟲ default" if lang == "en" else "⟲ Standard",
                callback_data=f"e:s:{worker}:_clear",
            )])
            prompt = (
                f"Effort für *{worker_label}* wählen:\nModell: `{model_for_worker}`"
                if lang == "de"
                else f"Pick effort for *{worker_label}*:\nModel: `{model_for_worker}`"
            )
        else:
            # Ollama: temperature instead. Curated set (creativity vs determinism).
            temps = (0.0, 0.2, 0.5, 0.7, 1.0)
            # Convert 'planner' worker key → DB column key 'planner_temperature' etc.;
            # 'triage' UI worker maps to 'chat_temperature'.
            tcb = "chat" if worker == "triage" else worker
            buttons = [
                [InlineKeyboardButton(f"{t:.1f}", callback_data=f"e:t:{tcb}:{t}")]
                for t in temps
            ]
            buttons.append([InlineKeyboardButton(
                "⟲ default" if lang == "en" else "⟲ Standard",
                callback_data=f"e:t:{tcb}:_clear",
            )])
            prompt = (
                f"Temperature für *{worker_label}* wählen:\n"
                f"Modell: `{model_for_worker}` _(Ollama)_\n"
                "_Niedrig = deterministisch, hoch = kreativ._"
                if lang == "de"
                else f"Pick temperature for *{worker_label}*:\n"
                     f"Model: `{model_for_worker}` _(Ollama)_\n"
                     "_Low = deterministic, high = creative._"
            )

        buttons.append([InlineKeyboardButton(
            "← Back" if lang == "en" else "← Zurück", callback_data="e:back",
        )])
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
        text, kb = effort_main_view(lang, sess, s)
        shown = value or ("default" if lang == "en" else "Standard")
        confirm = f"✅ {worker} effort → `{shown}`\n\n{text}"
        await q.edit_message_text(confirm, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    if data.startswith("e:t:"):
        _, _, worker, raw = data.split(":", 3)
        value: float | None = None if raw == "_clear" else float(raw)
        await store.set_chat_temperature(chat_id, worker, value)
        sess = await store.get_chat_session(chat_id) or {}
        s = settings()
        text, kb = effort_main_view(lang, sess, s)
        shown = "default" if value is None else f"{value:.2f}"
        confirm = f"✅ {worker} temperature → `{shown}`\n\n{text}"
        await q.edit_message_text(confirm, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return


# ---------- /replan ----------

def _fmt_budget(n: int | None, default_value: int) -> str:
    if n is None:
        return f"default ({_fmt_budget_value(default_value)})"
    return _fmt_budget_value(n)


def _fmt_budget_value(n: int) -> str:
    if n >= UNLIMITED_SENTINEL:
        return "∞"
    return str(n)


async def cmd_replan(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    sess = await store.get_chat_session(update.effective_chat.id) or {}
    s = settings()
    cur = sess.get("replan_max")
    cur_display = _fmt_budget(cur, s.cascade_replan_max)

    args = ctx.args or []
    if not args:
        if lang == "de":
            head = (
                f"*Replan-Budget* — Anzahl Replans wenn Loop steckenbleibt.\n"
                f"Aktuell: `{cur_display}`\n\n"
                f"Wähle eine Stufe, 'Custom' für Eigenwert (`/replan <n>`) oder Standard:"
            )
        else:
            head = (
                f"*Replan budget* — how often the planner can rewrite the plan when stuck.\n"
                f"Current: `{cur_display}`\n\n"
                f"Pick a level, 'Custom' for a custom value (`/replan <n>`), or default:"
            )
        buttons = []
        for n in REPLAN_CHOICES:
            if n == 0:
                label = "0 — aus" if lang == "de" else "0 — off"
            elif n >= UNLIMITED_SENTINEL:
                label = "∞ — unbegrenzt" if lang == "de" else "∞ — unlimited"
            else:
                label = f"{n}×"
            buttons.append([InlineKeyboardButton(label, callback_data=f"r:s:{n}")])
        buttons.append([InlineKeyboardButton(
            "✏️ Custom" if lang == "en" else "✏️ Eigenwert",
            callback_data="r:custom",
        )])
        buttons.append([InlineKeyboardButton(
            "⟲ Standard" if lang == "de" else "⟲ default", callback_data="r:s:_clear",
        )])
        await update.effective_message.reply_text(
            head, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    try:
        n = int(args[0])
        if n < 0 or n > UNLIMITED_SENTINEL:
            raise ValueError("out of range")
    except ValueError:
        await update.effective_message.reply_text(
            f"Aufruf: /replan <n>  (n=0..{UNLIMITED_SENTINEL}; {UNLIMITED_SENTINEL} = unbegrenzt)"
            if lang == "de"
            else f"Usage: /replan <n>  (n=0..{UNLIMITED_SENTINEL}; {UNLIMITED_SENTINEL} = unlimited)"
        )
        return
    await store.set_chat_replan_max(update.effective_chat.id, n)
    shown = _fmt_budget_value(n)
    await update.effective_message.reply_text(
        f"✅ Replan-Budget = `{shown}`" if lang == "de" else f"✅ Replan budget = `{shown}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_iterations(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    sess = await store.get_chat_session(update.effective_chat.id) or {}
    s = settings()
    cur = sess.get("max_iterations")
    cur_display = _fmt_budget(cur, s.cascade_max_iterations)

    args = ctx.args or []
    if not args:
        if lang == "de":
            head = (
                f"*Max-Iterationen pro Run* — hartes Cap, unabhängig vom Replan-Budget.\n"
                f"Aktuell: `{cur_display}`\n\n"
                f"Wähle eine Stufe, 'Custom' (`/iterations <n>`) oder Standard:"
            )
        else:
            head = (
                f"*Max iterations per run* — hard cap, independent of replan budget.\n"
                f"Current: `{cur_display}`\n\n"
                f"Pick a level, 'Custom' (`/iterations <n>`), or default:"
            )
        buttons = []
        for n in ITERATION_CHOICES:
            if n >= UNLIMITED_SENTINEL:
                label = "∞ — unbegrenzt" if lang == "de" else "∞ — unlimited"
            else:
                label = f"{n}×"
            buttons.append([InlineKeyboardButton(label, callback_data=f"i:s:{n}")])
        buttons.append([InlineKeyboardButton(
            "✏️ Custom" if lang == "en" else "✏️ Eigenwert",
            callback_data="i:custom",
        )])
        buttons.append([InlineKeyboardButton(
            "⟲ Standard" if lang == "de" else "⟲ default", callback_data="i:s:_clear",
        )])
        await update.effective_message.reply_text(
            head, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    try:
        n = int(args[0])
        if n < 1 or n > UNLIMITED_SENTINEL:
            raise ValueError("out of range")
    except ValueError:
        await update.effective_message.reply_text(
            f"Aufruf: /iterations <n>  (n=1..{UNLIMITED_SENTINEL}; {UNLIMITED_SENTINEL} = unbegrenzt)"
            if lang == "de"
            else f"Usage: /iterations <n>  (n=1..{UNLIMITED_SENTINEL}; {UNLIMITED_SENTINEL} = unlimited)"
        )
        return
    await store.set_chat_max_iterations(update.effective_chat.id, n)
    shown = _fmt_budget_value(n)
    await update.effective_message.reply_text(
        f"✅ Max-Iterationen = `{shown}`" if lang == "de" else f"✅ Max iterations = `{shown}`",
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
    if data == "r:custom":
        msg = (
            f"✏️ Tippe `/replan <n>` für einen Eigenwert (z.B. `/replan 7`). "
            f"Bereich 0..{UNLIMITED_SENTINEL}. {UNLIMITED_SENTINEL} = unbegrenzt."
            if lang == "de"
            else
            f"✏️ Type `/replan <n>` for a custom value (e.g. `/replan 7`). "
            f"Range 0..{UNLIMITED_SENTINEL}. {UNLIMITED_SENTINEL} = unlimited."
        )
        await q.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        return
    if data.startswith("r:s:"):
        raw = data.split(":", 2)[2]
        if raw == "_clear":
            await store.set_chat_replan_max(chat_id, None)
            txt = "✅ Replan-Budget = Standard" if lang == "de" else "✅ Replan budget = default"
        else:
            n = int(raw)
            await store.set_chat_replan_max(chat_id, n)
            shown = _fmt_budget_value(n)
            txt = f"✅ Replan-Budget = `{shown}`" if lang == "de" else f"✅ Replan budget = `{shown}`"
        await q.edit_message_text(txt, parse_mode=ParseMode.MARKDOWN)


async def cmd_subtasks(update: Update, ctx) -> None:
    """Cap how many sub-tasks the planner is allowed to emit per run."""
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    sess = await store.get_chat_session(update.effective_chat.id) or {}
    s = settings()
    cur = sess.get("max_subtasks")
    cur_display = str(cur) if cur is not None else f"default ({s.cascade_max_subtasks})"

    args = ctx.args or []
    if not args:
        if lang == "de":
            head = (
                f"*Max Sub-Tasks pro Run* — Limit für Auto-Decompose.\n"
                f"Aktuell: `{cur_display}`\n\n"
                f"Wähle eine Stufe oder nutze `/subtasks <n>` (n=1..20):"
            )
        else:
            head = (
                f"*Max sub-tasks per run* — cap for auto-decompose.\n"
                f"Current: `{cur_display}`\n\n"
                f"Pick a level or use `/subtasks <n>` (n=1..20):"
            )
        buttons = [
            [InlineKeyboardButton(f"{n}×", callback_data=f"st:s:{n}")]
            for n in (1, 3, 5, 8, 12)
        ]
        buttons.append([InlineKeyboardButton(
            "✏️ Custom" if lang == "en" else "✏️ Eigenwert", callback_data="st:custom",
        )])
        buttons.append([InlineKeyboardButton(
            "⟲ Standard" if lang == "de" else "⟲ default", callback_data="st:s:_clear",
        )])
        await update.effective_message.reply_text(
            head, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons)
        )
        return
    try:
        n = int(args[0])
        if n < 1 or n > 20:
            raise ValueError()
    except ValueError:
        await update.effective_message.reply_text(
            "Aufruf: /subtasks <n>  (n=1..20)" if lang == "de"
            else "Usage: /subtasks <n>  (n=1..20)"
        )
        return
    await store.set_chat_int_setting(update.effective_chat.id, "max_subtasks", n)
    await update.effective_message.reply_text(
        f"✅ Max Sub-Tasks = `{n}`" if lang == "de" else f"✅ Max sub-tasks = `{n}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def on_subtasks_callback(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    q = update.callback_query
    await q.answer()
    lang = lang_for(update)
    data = q.data or ""
    store: Store = ctx.application.bot_data["store"]
    chat_id = update.effective_chat.id
    if data == "st:custom":
        msg = (
            "✏️ Tippe `/subtasks <n>` (n=1..20)."
            if lang == "de" else "✏️ Type `/subtasks <n>` (n=1..20)."
        )
        await q.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        return
    if data.startswith("st:s:"):
        raw = data.split(":", 2)[2]
        if raw == "_clear":
            await store.set_chat_int_setting(chat_id, "max_subtasks", None)
            txt = "✅ Max Sub-Tasks = Standard" if lang == "de" else "✅ Max sub-tasks = default"
        else:
            n = int(raw)
            await store.set_chat_int_setting(chat_id, "max_subtasks", n)
            txt = f"✅ Max Sub-Tasks = `{n}`" if lang == "de" else f"✅ Max sub-tasks = `{n}`"
        await q.edit_message_text(txt, parse_mode=ParseMode.MARKDOWN)


async def cmd_failsbeforereplan(update: Update, ctx) -> None:
    """How many consecutive reviewer-fails trigger an auto-replan."""
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    sess = await store.get_chat_session(update.effective_chat.id) or {}
    s = settings()
    cur = sess.get("replan_after_failures")
    cur_display = (
        str(cur) if cur is not None else f"default ({s.cascade_replan_after_failures})"
    )

    args = ctx.args or []
    if not args:
        if lang == "de":
            head = (
                f"*Fails vor Auto-Replan* — wie viele aufeinanderfolgende "
                f"Reviewer-Fails den Planner-Replan auslösen.\n"
                f"Aktuell: `{cur_display}`\n\n"
                f"Wähle eine Stufe oder nutze `/failsbeforereplan <n>` (n=1..10):"
            )
        else:
            head = (
                f"*Fails before auto-replan* — how many consecutive reviewer "
                f"fails trigger a planner replan.\n"
                f"Current: `{cur_display}`\n\n"
                f"Pick a level or use `/failsbeforereplan <n>` (n=1..10):"
            )
        buttons = [
            [InlineKeyboardButton(f"{n}×", callback_data=f"f:s:{n}")]
            for n in (1, 2, 3, 5)
        ]
        buttons.append([InlineKeyboardButton(
            "✏️ Custom" if lang == "en" else "✏️ Eigenwert",
            callback_data="f:custom",
        )])
        buttons.append([InlineKeyboardButton(
            "⟲ Standard" if lang == "de" else "⟲ default", callback_data="f:s:_clear",
        )])
        await update.effective_message.reply_text(
            head, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    try:
        n = int(args[0])
        if n < 1 or n > 10:
            raise ValueError()
    except ValueError:
        await update.effective_message.reply_text(
            "Aufruf: /failsbeforereplan <n>  (n=1..10)"
            if lang == "de"
            else "Usage: /failsbeforereplan <n>  (n=1..10)"
        )
        return
    await store.set_chat_int_setting(
        update.effective_chat.id, "replan_after_failures", n,
    )
    await update.effective_message.reply_text(
        f"✅ Fails vor Replan = `{n}`" if lang == "de" else f"✅ Fails before replan = `{n}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def on_failsbeforereplan_callback(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    q = update.callback_query
    await q.answer()
    lang = lang_for(update)
    data = q.data or ""
    store: Store = ctx.application.bot_data["store"]
    chat_id = update.effective_chat.id
    if data == "f:custom":
        msg = (
            "✏️ Tippe `/failsbeforereplan <n>` (n=1..10)."
            if lang == "de"
            else "✏️ Type `/failsbeforereplan <n>` (n=1..10)."
        )
        await q.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        return
    if data.startswith("f:s:"):
        raw = data.split(":", 2)[2]
        if raw == "_clear":
            await store.set_chat_int_setting(chat_id, "replan_after_failures", None)
            txt = "✅ Fails vor Replan = Standard" if lang == "de" else "✅ Fails before replan = default"
        else:
            n = int(raw)
            await store.set_chat_int_setting(chat_id, "replan_after_failures", n)
            txt = (
                f"✅ Fails vor Replan = `{n}`" if lang == "de"
                else f"✅ Fails before replan = `{n}`"
            )
        await q.edit_message_text(txt, parse_mode=ParseMode.MARKDOWN)


# ---------- /toggles ----------

_TOGGLE_KEYS: list[tuple[str, str, str, str]] = [
    # (db_column, settings_attr, label_de, label_en)
    ("triage_enabled",      "cascade_triage_enabled",      "🧭 Triage / Dispatcher",      "🧭 Triage / dispatcher"),
    ("auto_skill_suggest",  "cascade_auto_skill_suggest",  "💡 Auto-Skill-Vorschläge",   "💡 Auto-skill-suggestions"),
    ("context7_enabled",    "cascade_context7_enabled",    "📚 Context7 (Library-Docs)", "📚 Context7 (library docs)"),
    ("websearch_enabled",   "cascade_websearch_enabled",   "🌐 Web-Suche (Brave)",       "🌐 Web search (Brave)"),
    ("auto_decompose",      "cascade_auto_decompose",      "🪓 Auto-Decompose (Sub-Tasks)", "🪓 Auto-decompose (sub-tasks)"),
    ("multiplan_enabled",   "cascade_multiplan_enabled",   "🗳️ Multi-Plan-Voting (2× Planner)", "🗳️ Multi-plan voting (2× planner)"),
]


def _toggle_view(lang: str, sess: dict, s):
    sess = sess or {}
    head = (
        "*Feature-Toggles*\nKlick auf eine Zeile zum Umschalten:"
        if lang == "de"
        else "*Feature toggles*\nClick a row to flip:"
    )
    rows = []
    text_lines = [head, ""]
    for col, attr, dlabel, elabel in _TOGGLE_KEYS:
        label = dlabel if lang == "de" else elabel
        override = sess.get(col)
        eff = bool(override) if override is not None else bool(getattr(s, attr))
        marker = "✅" if eff else "❌"
        src = "(per-Chat)" if override is not None else "(default)"
        if lang == "en":
            src = "(per-chat)" if override is not None else "(default)"
        text_lines.append(f"{marker} {label} `{src}`")
        rows.append([InlineKeyboardButton(
            f"{marker} {label}", callback_data=f"tg:flip:{col}",
        )])
    rows.append([InlineKeyboardButton(
        "⟲ Alle auf Standard" if lang == "de" else "⟲ All to default",
        callback_data="tg:reset_all",
    )])
    rows.append([InlineKeyboardButton(
        "✖ Schliessen" if lang == "de" else "✖ Close", callback_data="tg:close",
    )])
    return "\n".join(text_lines), InlineKeyboardMarkup(rows)


async def cmd_toggles(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    sess = await store.get_chat_session(update.effective_chat.id) or {}
    text, kb = _toggle_view(lang, sess, settings())
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
    )


async def on_toggles_callback(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    q = update.callback_query
    await q.answer()
    lang = lang_for(update)
    data = q.data or ""
    store: Store = ctx.application.bot_data["store"]
    chat_id = update.effective_chat.id
    s = settings()

    if data == "tg:close":
        await q.edit_message_text("✓" if lang == "en" else "✓ Geschlossen.")
        return
    if data == "tg:reset_all":
        for col, _attr, _dl, _el in _TOGGLE_KEYS:
            await store.set_chat_int_setting(chat_id, col, None)
        sess = await store.get_chat_session(chat_id) or {}
        text, kb = _toggle_view(lang, sess, s)
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return
    if data.startswith("tg:flip:"):
        col = data.split(":", 2)[2]
        valid = {c for c, *_ in _TOGGLE_KEYS}
        if col not in valid:
            return
        sess = await store.get_chat_session(chat_id) or {}
        attr = next(a for c, a, *_ in _TOGGLE_KEYS if c == col)
        cur_override = sess.get(col)
        eff = bool(cur_override) if cur_override is not None else bool(getattr(s, attr))
        await store.set_chat_int_setting(chat_id, col, 0 if eff else 1)
        sess = await store.get_chat_session(chat_id) or {}
        text, kb = _toggle_view(lang, sess, s)
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return


async def on_iterations_callback(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    q = update.callback_query
    await q.answer()
    lang = lang_for(update)
    data = q.data or ""
    store: Store = ctx.application.bot_data["store"]
    chat_id = update.effective_chat.id
    if data == "i:custom":
        msg = (
            f"✏️ Tippe `/iterations <n>` für einen Eigenwert (z.B. `/iterations 15`). "
            f"Bereich 1..{UNLIMITED_SENTINEL}. {UNLIMITED_SENTINEL} = unbegrenzt."
            if lang == "de"
            else
            f"✏️ Type `/iterations <n>` for a custom value (e.g. `/iterations 15`). "
            f"Range 1..{UNLIMITED_SENTINEL}. {UNLIMITED_SENTINEL} = unlimited."
        )
        await q.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        return
    if data.startswith("i:s:"):
        raw = data.split(":", 2)[2]
        if raw == "_clear":
            await store.set_chat_max_iterations(chat_id, None)
            txt = "✅ Max-Iterationen = Standard" if lang == "de" else "✅ Max iterations = default"
        else:
            n = int(raw)
            await store.set_chat_max_iterations(chat_id, n)
            shown = _fmt_budget_value(n)
            txt = f"✅ Max-Iterationen = `{shown}`" if lang == "de" else f"✅ Max iterations = `{shown}`"
        await q.edit_message_text(txt, parse_mode=ParseMode.MARKDOWN)
