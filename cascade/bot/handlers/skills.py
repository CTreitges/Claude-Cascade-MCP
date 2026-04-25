"""Skill commands: /skills /run + the suggestion accept/reject callback."""

from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode

from cascade.store import Store

from ..helpers import lang_for, owner_only
from ..runner import run_task_for_chat
from ..state import PENDING_SKILL


async def cmd_skills(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    args = ctx.args or []
    if args and args[0] == "delete" and len(args) >= 2:
        ok = await store.delete_skill(args[1])
        if lang == "de":
            await update.effective_message.reply_text("✅ Gelöscht." if ok else "Skill nicht gefunden.")
        else:
            await update.effective_message.reply_text("✅ Deleted." if ok else "Skill not found.")
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
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
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
    text = template
    kv = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in params if "=" in p}
    rest = [p for p in params if "=" not in p]
    try:
        text = template.format(*rest, **kv)
    except (KeyError, IndexError):
        text = template + ("\n\n" + " ".join(params) if params else "")
    await store.increment_skill_usage(name)
    await run_task_for_chat(update, ctx, text)


async def on_skill_callback(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    chat_id = update.effective_chat.id

    if data.startswith("sk:y:"):
        name = data.split(":", 2)[2]
        sug = PENDING_SKILL.pop(chat_id, None)
        if not sug or sug.get("name") != name:
            await q.edit_message_text(
                "⚠ Vorschlag nicht mehr verfügbar." if lang == "de"
                else "⚠ Suggestion no longer available."
            )
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
            from cascade.memory import remember_decision
            await remember_decision(
                f"New skill saved: '{name}' — {sug.get('description') or ''}. "
                f"Template: {sug.get('task_template', '')[:200]}",
                importance="high", tags="claude-cascade,skill,user-accepted",
            )
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
        sug = PENDING_SKILL.pop(chat_id, None)
        if sug and sug.get("task_id"):
            await store.mark_skill_suggestion_decided(sug["task_id"], "rejected")
        await q.edit_message_text("Verworfen." if lang == "de" else "Discarded.")
        return
