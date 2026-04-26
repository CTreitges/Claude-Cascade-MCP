"""Inline-keyboard for resume confirmation.

When the user submits a free-form task that matches an interrupted-status
task in similarity, runner.py opens a small dialog instead of dispatching
blindly. This module owns the keyboard + the callback router.

Decision values stored in `PENDING_RESUME` futures:
  - "resume"   → continue the existing task (resume_task_id)
  - "fresh"    → start a new task on the same workspace
  - "abort"    → don't run anything
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..helpers import owner_only
from ..state import PENDING_RESUME

log = logging.getLogger("cascade.bot.resume_kbd")


def make_keyboard(callback_id: str, lang: str = "de") -> InlineKeyboardMarkup:
    if lang == "de":
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Fortsetzen", callback_data=f"resume:{callback_id}:resume"),
            InlineKeyboardButton("🆕 Neu starten", callback_data=f"resume:{callback_id}:fresh"),
            InlineKeyboardButton("🚫 Abbrechen", callback_data=f"resume:{callback_id}:abort"),
        ]])
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("▶️ Continue", callback_data=f"resume:{callback_id}:resume"),
        InlineKeyboardButton("🆕 Restart", callback_data=f"resume:{callback_id}:fresh"),
        InlineKeyboardButton("🚫 Cancel", callback_data=f"resume:{callback_id}:abort"),
    ]])


async def on_resume_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_only(update, ctx):
        return
    cq = update.callback_query
    if not cq or not cq.data:
        return
    parts = cq.data.split(":", 2)
    if len(parts) != 3 or parts[0] != "resume":
        return
    callback_id, decision = parts[1], parts[2]
    fut = PENDING_RESUME.pop(callback_id, None)
    if fut is None or fut.done():
        await cq.answer("(abgelaufen)")
        return
    fut.set_result(decision)
    label = {
        "resume": "▶️ Fortsetzen",
        "fresh":  "🆕 Neu starten",
        "abort":  "🚫 Abgebrochen",
    }.get(decision, decision)
    try:
        await cq.answer(label)
        # Strip the keyboard so the user can't tap twice.
        await cq.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        log.debug("could not strip keyboard: %s", e)
