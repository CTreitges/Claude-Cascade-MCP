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
    store: Store = ctx.application.bot_data["store"]
    args = ctx.args or []
    target_id: str | None = args[0] if args else None
    from ..state import TASK_REGISTRY

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

    # Look up cancel_event by task-id. TASK_REGISTRY is the authoritative
    # source — it tracks every running task even when INFLIGHT[chat] got
    # overwritten by a newer task in the same chat.
    ev = TASK_REGISTRY.get(target_id)
    if ev is None:
        for _cid, (tid, _t, ev2) in list(INFLIGHT.items()):
            if tid == target_id:
                ev = ev2
                break
    if ev is not None:
        ev.set()
        try:
            await store.update_task(
                target_id, status="cancelled",
                result_summary="cancelled via /cancel", completed=True,
            )
        except Exception:
            pass
        await update.effective_message.reply_text(
            t("cancel_sent", lang=lang, task_id=target_id), parse_mode=ParseMode.MARKDOWN
        )
        return

    # Not in our process — orphan task in DB? Mark cancelled so the user
    # sees a clean state instead of a stale "running" task forever.
    try:
        db_task = await store.get_task(target_id)
    except Exception:
        db_task = None
    if db_task and db_task.status in ("running", "queued", "interrupted"):
        await store.update_task(
            target_id, status="cancelled",
            result_summary="cancelled via /cancel (orphan)", completed=True,
        )
        await update.effective_message.reply_text(
            (f"🚫 Task `{target_id}` war kein aktiver In-Process-Task, "
             f"DB-Status auf `cancelled` gesetzt.")
            if lang == "de"
            else (f"🚫 Task `{target_id}` wasn't active in this process; "
                  f"DB status set to `cancelled`."),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.effective_message.reply_text(
        t("cancel_not_running", lang=lang, task_id=target_id), parse_mode=ParseMode.MARKDOWN
    )


async def on_wait_callback(update: Update, ctx) -> None:
    """Inline-keyboard router for the wait-for-session prompt.
    callback_data: `wait:<task_id>:abort` or `wait:<task_id>:keep`.

    Ties the user's "abort vs keep waiting" choice into the task's
    cancel_event held in TASK_REGISTRY. No new Telegram round-trip
    is needed — flipping the event makes with_retry's _wait_with_cancel
    return immediately and the cascade unwinds with a clean cancel.
    """
    if not await owner_only(update, ctx):
        return
    q = update.callback_query
    if not q or not q.data:
        return
    parts = q.data.split(":", 2)
    if len(parts) != 3 or parts[0] != "wait":
        return
    target_id, decision = parts[1], parts[2]
    lang = lang_for(update)

    from ..state import TASK_REGISTRY
    ev = TASK_REGISTRY.get(target_id)

    if decision == "keep":
        await q.answer("OK — warte weiter" if lang == "de" else "OK — keep waiting")
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    # decision == "abort"
    if ev is None:
        await q.answer(
            "Task läuft nicht mehr in diesem Prozess"
            if lang == "de"
            else "Task no longer running in this process"
        )
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    ev.set()
    store: Store = ctx.application.bot_data["store"]
    try:
        await store.update_task(
            target_id, status="cancelled",
            result_summary="cancelled by user via wait-keyboard",
            completed=True,
        )
    except Exception:
        pass
    await q.answer("✋ Abbruch gesendet" if lang == "de" else "✋ Abort sent")
    try:
        await q.edit_message_reply_markup(reply_markup=None)
        await q.edit_message_text(
            f"✋ Task `{target_id}` wird abgebrochen."
            if lang == "de"
            else f"✋ Task `{target_id}` aborting.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass


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
    # Extra text after the task id is treated as an additional hint that
    # gets prepended to the original task_text — this nudges the resumed
    # planner / implementer in a new direction without losing workspace state.
    extra = " ".join(args[1:]).strip()
    if extra:
        hint_label = (
            "ZUSÄTZLICHER HINWEIS FÜR DIESEN RESUME-LAUF"
            if lang == "de"
            else "ADDITIONAL HINT FOR THIS RESUME RUN"
        )
        task_text = f"{task.task_text}\n\n--- {hint_label} ---\n{extra}"
        await update.effective_message.reply_text(
            f"🔁 Resume `{task.id}` mit Hinweis: _{extra[:200]}_"
            if lang == "de"
            else f"🔁 Resuming `{task.id}` with hint: _{extra[:200]}_",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        task_text = task.task_text
    await run_task_for_chat(update, ctx, task_text, resume_task_id=task.id)


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


async def cmd_wait(update: Update, ctx) -> None:
    """Show which running tasks are currently waiting on a rate-limit
    / session window, plus the estimated next-availability per task.

    Reads the most recent `waiting_for_session` event from each in-flight
    task's log table — that's where `with_retry` (via the WAIT_NOTIFIER
    contextvar) emits its "sleeping for N seconds" pings.
    """
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    if not INFLIGHT:
        await update.effective_message.reply_text(
            "Nichts läuft — also nichts wartet."
            if lang == "de"
            else "Nothing in flight — nothing is waiting.",
        )
        return
    import re
    import time as _t
    lines = [
        "*Aktive Wait-Status:*" if lang == "de" else "*Active wait status:*",
    ]
    any_waiting = False
    for cid, (tid, _task, _ev) in INFLIGHT.items():
        try:
            entries = await store.tail_logs(tid, n=120)
        except Exception:
            entries = []
        # Look backwards for the most recent "waiting_for_session" line.
        last = None
        for e in reversed(entries):
            if "waiting_for_session" in (e.message or ""):
                last = e
                break
        if last is None:
            lines.append(
                f"  ▶️ `{tid}` (chat `{cid}`) — {'arbeitet' if lang == 'de' else 'working'}"
            )
            continue
        any_waiting = True
        m = re.search(r'"seconds"\s*:\s*(\d+)', last.message or "")
        secs = int(m.group(1)) if m else 0
        elapsed = max(0, int(_t.time() - last.ts))
        remaining = max(0, secs - elapsed)
        if remaining >= 86400:
            when = f"~{remaining // 86400}T {(remaining % 86400) // 3600}h"
        elif remaining >= 3600:
            when = f"~{remaining // 3600}h {(remaining % 3600) // 60}min"
        elif remaining >= 60:
            when = f"~{remaining // 60}min {remaining % 60}s"
        else:
            when = f"{remaining}s"
        reason_match = re.search(r'"reason"\s*:\s*"([^"]+)"', last.message or "")
        reason = reason_match.group(1) if reason_match else "?"
        lines.append(
            f"  ⏳ `{tid}` (chat `{cid}`) — {when} ({reason[:80]})"
        )
    if not any_waiting:
        # Replace the header for readability.
        lines[0] = (
            "Alle laufenden Tasks arbeiten — niemand wartet."
            if lang == "de"
            else "All running tasks are working — none waiting."
        )
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_abort(update: Update, ctx) -> None:
    """Abort EVERYTHING running — both in this process AND any DB-orphan
    tasks left over from an earlier crash."""
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    from ..state import TASK_REGISTRY

    stopped: list[str] = []
    for tid, ev in list(TASK_REGISTRY.items()):
        ev.set()
        stopped.append(tid)
    for _cid, (tid, _task, ev) in list(INFLIGHT.items()):
        if tid not in stopped:
            ev.set()
            stopped.append(tid)

    # DB sweep — covers tasks orphaned by an earlier bot crash.
    all_tasks = await store.list_tasks(limit=200)
    db_active = [
        t_ for t_ in all_tasks
        if t_.status in ("running", "queued", "interrupted")
    ]
    db_cancelled: list[str] = []
    for t_ in db_active:
        try:
            await store.update_task(
                t_.id, status="cancelled",
                result_summary="cancelled via /abort", completed=True,
            )
            if t_.id not in stopped:
                db_cancelled.append(t_.id)
        except Exception:
            pass

    if not stopped and not db_cancelled:
        await update.effective_message.reply_text(
            "Nichts zu abbrechen." if lang == "de" else "Nothing to abort."
        )
        return

    parts = []
    if stopped:
        parts.append(
            (f"🚫 {len(stopped)} laufende Task(s) abgebrochen:"
             if lang == "de"
             else f"🚫 Aborted {len(stopped)} running task(s):")
        )
        parts.extend(f"  • `{tid}`" for tid in stopped)
    if db_cancelled:
        parts.append(
            (f"🗑 {len(db_cancelled)} Orphan-Task(s) im DB-Status auf `cancelled`:"
             if lang == "de"
             else f"🗑 {len(db_cancelled)} orphan task(s) marked `cancelled` in DB:")
        )
        parts.extend(f"  • `{tid}`" for tid in db_cancelled)
    await update.effective_message.reply_text(
        "\n".join(parts), parse_mode=ParseMode.MARKDOWN
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
