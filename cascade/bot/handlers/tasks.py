"""Task lifecycle commands: status / logs / cancel / history / resume / again /
diff / queue / abort / dryrun."""

from __future__ import annotations

import asyncio

from telegram import Update
from telegram.constants import ChatAction, ParseMode

from cascade.config import settings
from cascade.i18n import t
from cascade.store import Store

from ..helpers import (
    fmt_local,
    fmt_status_emoji,
    lang_for,
    owner_only,
    send,
    send_long,
)
from ..runner import run_task_for_chat
from ..state import INFLIGHT


async def cmd_status(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    args = ctx.args or []
    task = await store.get_task(args[0]) if args else await store.latest_task()
    if not task:
        await update.effective_message.reply_text(t("no_tasks", lang=lang))
        return
    await update.effective_message.reply_text(
        t(
            "status_line",
            lang=lang,
            emoji=fmt_status_emoji(task.status),
            status=task.status,
            task_id=task.id,
            task=task.task_text[:200],
            iteration=task.iteration,
            summary=task.result_summary or "—",
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_logs(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
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
    text = "\n".join(f"{fmt_local(e.ts)} [{e.level}] {e.message}" for e in entries)
    await send(update.effective_message, text, code=True)


async def cmd_cancel(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    args = ctx.args or []
    target_id: str | None = args[0] if args else None
    if target_id is None:
        chat_id = update.effective_chat.id
        if chat_id not in INFLIGHT:
            await update.effective_message.reply_text(t("no_inflight", lang=lang))
            return
        target_id, _, ev = INFLIGHT[chat_id]
        ev.set()
        await update.effective_message.reply_text(
            t("cancel_sent", lang=lang, task_id=target_id), parse_mode=ParseMode.MARKDOWN
        )
        return
    for _cid, (tid, _t, ev) in list(INFLIGHT.items()):
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
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    tasks = await store.list_tasks(limit=10)
    if not tasks:
        await update.effective_message.reply_text(t("no_tasks", lang=lang))
        return
    lines = []
    for task in tasks:
        ts = fmt_local(task.created_at, "%H:%M")
        lines.append(
            f"{fmt_status_emoji(task.status)} {ts} `{task.id}` "
            f"i={task.iteration} {task.task_text[:70]}"
        )
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_resume(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
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
    await run_task_for_chat(update, ctx, task.task_text, resume_task_id=task.id)


async def cmd_again(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    args = ctx.args or []
    task = await store.get_task(args[0]) if args else await store.latest_task()
    if not task:
        await update.effective_message.reply_text(
            "Kein Task gefunden." if lang == "de" else "No task found."
        )
        return
    await update.effective_message.reply_text(
        f"🔄 Wiederhole: {(task.task_text or '')[:200]}" if lang == "de"
        else f"🔄 Re-running: {(task.task_text or '')[:200]}"
    )
    await run_task_for_chat(update, ctx, task.task_text)


async def cmd_diff(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    args = ctx.args or []
    if args:
        tid = args[0]
    else:
        latest = await store.latest_task()
        if not latest:
            await update.effective_message.reply_text(
                "Kein Task." if lang == "de" else "No task."
            )
            return
        tid = latest.id
    iters = await store.list_iterations(tid)
    runtime = [i for i in iters if i.n > 0]
    if not runtime or not runtime[-1].diff_excerpt:
        await update.effective_message.reply_text(
            "Kein Diff vorhanden." if lang == "de" else "No diff stored."
        )
        return
    await send_long(update.effective_message, runtime[-1].diff_excerpt, code=True)


async def cmd_queue(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    if not INFLIGHT:
        await update.effective_message.reply_text(
            "Nichts läuft gerade." if lang == "de" else "Nothing in flight."
        )
        return
    lines = ["*Laufende Tasks:*" if lang == "de" else "*Running tasks:*"]
    for cid, (tid, _task, _ev) in INFLIGHT.items():
        lines.append(f"• `{tid}` (chat `{cid}`)")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_abort(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    if not INFLIGHT:
        await update.effective_message.reply_text(
            "Nichts zu abbrechen." if lang == "de" else "Nothing to abort."
        )
        return
    n = 0
    for _cid, (_tid, _task, ev) in list(INFLIGHT.items()):
        ev.set()
        n += 1
    await update.effective_message.reply_text(
        f"🚫 {n} Task(s) abgebrochen." if lang == "de"
        else f"🚫 Aborted {n} task(s)."
    )


async def cmd_dryrun(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    args_text = " ".join(ctx.args or []).strip()
    if not args_text:
        await update.effective_message.reply_text(
            "Aufruf: /dryrun <Aufgabe>" if lang == "de" else "Usage: /dryrun <task>"
        )
        return
    s = settings()
    store: Store = ctx.application.bot_data["store"]
    sess = await store.get_chat_session(update.effective_chat.id) or {}
    if sess.get("planner_model"):
        s = s.model_copy(update={"cascade_planner_model": sess["planner_model"]})
    if sess.get("planner_effort"):
        s = s.model_copy(update={"cascade_planner_effort": sess["planner_effort"]})

    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    msg = await update.effective_message.reply_text(
        "🧠 Plane (dry-run, ohne Implementer)…" if lang == "de"
        else "🧠 Planning (dry-run, no implementer)…"
    )

    try:
        from cascade.agents.planner import call_planner
        from cascade.repo_resolver import discover_local_repos, repos_for_planner_prompt
        repos = await asyncio.to_thread(discover_local_repos)
        block = repos_for_planner_prompt(repos, args_text)
        plan = await call_planner(args_text, repo_candidates_block=block, s=s)
    except Exception as e:
        await msg.edit_text(f"❌ {e}")
        return

    parts = [
        "🧠 *Dry-Run-Plan*" if lang == "de" else "🧠 *Dry-Run plan*",
        f"\n*Summary:* {plan.summary}",
        "\n*Steps:*\n" + "\n".join(f"  • {step}" for step in plan.steps),
        "\n*Files:* " + (", ".join(f"`{f}`" for f in plan.files_to_touch) or "—"),
        "\n*Acceptance:*\n" + "\n".join(f"  • {a}" for a in plan.acceptance_criteria),
    ]
    if plan.quality_checks:
        parts.append("\n*Quality-Checks:*")
        for c in plan.quality_checks:
            parts.append(f"  • `{c.name}`: `{c.command}`")
    parts.append(
        f"\n*Repo:* `{plan.repo.kind}`"
        + (f" → `{plan.repo.path}`" if plan.repo.path else "")
        + (f" (clone {plan.repo.url})" if plan.repo.url else "")
    )
    full = "\n".join(parts)
    if len(full) > 3800:
        full = full[:3800] + "…"
    await msg.edit_text(full, parse_mode=ParseMode.MARKDOWN)
