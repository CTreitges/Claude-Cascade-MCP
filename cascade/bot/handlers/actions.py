"""Quick-action callbacks under a finished result message."""

from __future__ import annotations

from telegram import Update

from cascade.store import Store

from ..helpers import lang_for, owner_only, send_long
from ..runner import run_task_for_chat


async def on_action_callback(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "act":
        return
    action, tid = parts[1], parts[2]
    store: Store = ctx.application.bot_data["store"]
    lang = lang_for(update)

    if action == "again":
        task = await store.get_task(tid)
        if not task:
            await q.edit_message_reply_markup(reply_markup=None)
            return
        # Fresh re-run with "lessons learned": if the previous run didn't
        # finish cleanly, harvest the last reviewer feedback and prepend it
        # to the task text so the brand-new planner starts already aware of
        # what tripped up the previous attempt.
        augmented = task.task_text or ""
        lessons = ""
        if task.status in ("failed", "interrupted", "cancelled"):
            iters = await store.list_iterations(tid)
            for it in reversed(iters):
                if it.reviewer_feedback and it.reviewer_pass is False:
                    lessons = it.reviewer_feedback.strip()
                    break
            if lessons:
                hint_header = (
                    "ERFAHRUNGEN AUS DEM LETZTEN VERSUCH (vermeide diese Fehler)"
                    if lang == "de"
                    else "LESSONS FROM PREVIOUS ATTEMPT (avoid these mistakes)"
                )
                augmented = (
                    f"{task.task_text}\n\n"
                    f"--- {hint_header} ---\n{lessons[:1500]}"
                )
        if lessons:
            await q.message.reply_text(
                f"🔄 Neuer Anlauf mit Lessons Learned aus Task `{tid}`."
                if lang == "de"
                else f"🔄 Fresh attempt with lessons learned from task `{tid}`."
            )
        else:
            await q.message.reply_text(
                f"🔄 Wiederhole: {(task.task_text or '')[:200]}" if lang == "de"
                else f"🔄 Re-running: {(task.task_text or '')[:200]}"
            )
        await run_task_for_chat(update, ctx, augmented)
        return
    if action == "diff":
        iters = await store.list_iterations(tid)
        runtime = [i for i in iters if i.n > 0]
        if not runtime or not runtime[-1].diff_excerpt:
            await q.message.reply_text(
                "Kein Diff vorhanden." if lang == "de" else "No diff stored."
            )
            return
        await send_long(q.message, runtime[-1].diff_excerpt, code=True)
        return
    if action == "resume":
        task = await store.get_task(tid)
        if not task:
            return
        await run_task_for_chat(update, ctx, task.task_text, resume_task_id=tid)
        return
