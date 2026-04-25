"""The cascade-task runner: wires a Telegram update through to run_cascade,
manages the live status message, the heartbeat, and the post-run UI
(rich result, diff chunks, quick-action buttons, skill-suggestion prompt)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

from cascade.config import settings
from cascade.core import run_cascade
from cascade.i18n import t
from cascade.models import implementer_provider
from cascade.store import Store

from .helpers import (
    fmt_status_emoji,
    format_progress_line,
    lang_for,
    send_long,
)
from .state import INFLIGHT, PENDING_SKILL


async def run_task_for_chat(
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

    lang = lang_for(update)
    initial = t("progress.planning_initial", lang=lang)
    status_msg = await msg.reply_text(initial)
    cancel = asyncio.Event()

    state = {
        "lines": [initial],
        "skill_suggestion": None,
        "started_at": asyncio.get_event_loop().time(),
    }

    def _render() -> str:
        return "\n".join(state["lines"])

    async def progress(task_id: str, event: str, payload: dict) -> None:
        if event == "skill_suggested":
            state["skill_suggestion"] = {"task_id": task_id, **payload}
            return
        line = format_progress_line(event, payload, lang)
        if not line:
            return
        state["lines"].append(line)
        if len(state["lines"]) > 9:
            state["lines"] = [state["lines"][0], "  …"] + state["lines"][-7:]
        try:
            await status_msg.edit_text(_render())
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

    async def register_when_known() -> None:
        for _ in range(30):
            await asyncio.sleep(0.1)
            latest = await store.latest_task()
            if latest and latest.task_text == task_text:
                INFLIGHT[chat.id] = (latest.id, task_obj, cancel)
                if chat:
                    await store.set_chat_last_task(chat.id, latest.id)
                return

    asyncio.create_task(register_when_known())

    HB_MARKER = "​"

    async def heartbeat() -> None:
        spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        i = 0
        while not task_obj.done():
            await asyncio.sleep(30)
            if task_obj.done():
                return
            elapsed = int(asyncio.get_event_loop().time() - state["started_at"])
            mark = spinner[i % len(spinner)]
            i += 1
            label = "läuft" if lang == "de" else "running"
            tag = f"{HB_MARKER}  {mark} {label} {elapsed}s"
            try:
                await ctx.bot.send_chat_action(chat.id, ChatAction.TYPING)
                lines = state["lines"]
                if lines and lines[-1].startswith(HB_MARKER):
                    lines[-1] = tag
                else:
                    lines.append(tag)
                await status_msg.edit_text(_render())
            except Exception:
                pass

    hb_task = asyncio.create_task(heartbeat())

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
        hb_task.cancel()
        try:
            await hb_task
        except (asyncio.CancelledError, Exception):
            pass
        INFLIGHT.pop(chat.id, None)

    # Rich final report
    header = t(
        "result.summary",
        lang=lang,
        emoji=fmt_status_emoji(result.status),
        status=result.status,
        task_id=result.task_id,
        iterations=result.iterations,
        workspace=result.workspace_path,
        summary=result.summary,
    )
    parts = [header]
    if result.plan and result.plan.summary:
        label = "*Plan:*"
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

    if result.status in ("done", "failed") and result.task_id:
        action_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🔄 " + ("Nochmal" if lang == "de" else "Again"),
                callback_data=f"act:again:{result.task_id}",
            ),
            InlineKeyboardButton(
                "📄 Diff",
                callback_data=f"act:diff:{result.task_id}",
            ),
            InlineKeyboardButton(
                "🔁 Resume",
                callback_data=f"act:resume:{result.task_id}",
            ),
        ]])
        await msg.reply_text(full_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=action_kb)
    else:
        await msg.reply_text(full_msg, parse_mode=ParseMode.MARKDOWN)

    if result.diff and result.diff.strip():
        await send_long(msg, result.diff, code=True, chunk=3500)

    if result.status == "done" and state.get("skill_suggestion"):
        sug = state["skill_suggestion"]
        from cascade.skill_suggester import SkillSuggestion, format_skill_proposal
        try:
            sug_obj = SkillSuggestion.model_validate(
                {k: v for k, v in sug.items() if k != "task_id"} | {"should_create": True}
            )
            text = format_skill_proposal(sug_obj, lang=lang)
        except Exception:
            text = f"💡 Skill-Vorschlag: `{sug.get('name')}`"
        PENDING_SKILL[chat.id] = sug
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "💾 " + ("Speichern" if lang == "de" else "Save"),
                callback_data=f"sk:y:{sug['name']}",
            ),
            InlineKeyboardButton(
                "❌ " + ("Verwerfen" if lang == "de" else "Discard"),
                callback_data=f"sk:n:{sug['name']}",
            ),
        ]])
        await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
