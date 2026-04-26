"""Bot startup / shutdown — Application post_init / post_shutdown handlers."""

from __future__ import annotations

import logging

from telegram.constants import ParseMode
from telegram.ext import Application

from cascade.config import settings
from cascade.i18n import t
from cascade.store import Store

log = logging.getLogger("cascade.bot.lifecycle")


async def post_init(application: Application) -> None:
    s = settings()
    store = await Store.open(s.cascade_db_path)
    application.bot_data["store"] = store

    # Background chat-summariser: walks `chat_messages` for un-summarised
    # rows older than 7d and asks Sonnet to compress them. Best-effort —
    # crash here would NOT block the bot; we just log and skip.
    if getattr(s, "cascade_summarize_enabled", True):
        import asyncio
        from cascade.summarizer import background_loop
        stop_event = asyncio.Event()
        application.bot_data["summarizer_stop"] = stop_event
        application.bot_data["summarizer_task"] = asyncio.create_task(
            background_loop(
                store,
                tick_interval_s=float(getattr(s, "cascade_summarize_tick_s", 6 * 3600)),
                s=s,
                stop_event=stop_event,
            ),
        )

    # Restore per-chat lang preferences from DB into the in-memory cache so
    # the very first message after a bot restart already gets the right lang.
    try:
        from .state import LANG_OVERRIDE
        async with store._conn.execute(
            "SELECT chat_id, lang FROM sessions WHERE lang IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()
        for r in rows:
            LANG_OVERRIDE[int(r["chat_id"])] = r["lang"]
        if rows:
            log.info("restored %d lang preferences from DB", len(rows))
    except Exception as e:
        log.warning("could not warm lang cache: %s", e)

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

    # Auto-resume interrupted tasks after a small grace period so the bot has
    # finished booting (handlers registered, polling running). Each resume is
    # spawned as its own background task; failures are logged but don't kill
    # the bot. Settings.cascade_auto_resume_interrupted disables this.
    if interrupted and getattr(s, "cascade_auto_resume_interrupted", True):
        import asyncio
        from cascade.core import run_cascade

        async def _resume_one(task_id: str) -> None:
            try:
                await asyncio.sleep(15)  # grace: avoid racing with bot init
                task = await store.get_task(task_id)
                if task is None or task.status not in ("interrupted",):
                    return
                # Mark running again so /queue and /status see it.
                await store.update_task(task_id, status="running")
                log.info("auto-resuming interrupted task %s", task_id)
                if s.telegram_owner_id:
                    try:
                        await application.bot.send_message(
                            chat_id=s.telegram_owner_id,
                            text=f"🔁 Auto-Resume von Task `{task_id}` nach Restart…",
                            parse_mode=ParseMode.MARKDOWN,
                        )
                    except Exception:
                        pass
                from pathlib import Path
                repo = Path(task.workspace_path) if task.workspace_path else None
                await run_cascade(
                    task=task.task_text,
                    source=task.source,
                    repo=repo,
                    resume_task_id=task_id,
                    store=store,
                )
                if s.telegram_owner_id:
                    try:
                        latest = await store.get_task(task_id)
                        await application.bot.send_message(
                            chat_id=s.telegram_owner_id,
                            text=(
                                f"✅ Auto-Resume `{task_id}` → "
                                f"Status `{latest.status if latest else '?'}`"
                            ),
                            parse_mode=ParseMode.MARKDOWN,
                        )
                    except Exception:
                        pass
            except Exception as e:
                log.warning("auto-resume of %s failed: %s", task_id, e)

        for tid in interrupted:
            asyncio.create_task(_resume_one(tid))


async def post_shutdown(application: Application) -> None:
    """Graceful shutdown.

    The plan-Phase-9 fix for "systemd SIGKILL kills cascades mid-run":
      1. Mark all currently-running tasks as `interrupted` so /resume can
         pick them up cleanly on the next start.
      2. Wait up to 30s for in-flight handlers to finish their current
         step (sending a message, persisting an iteration). After that,
         systemd's TimeoutStopSec (now 180s) and KillMode=mixed handle
         the rest.
      3. Close the store last — anything else that wants to log to it
         must finish above this line.
    """
    import asyncio

    # Stop the background summariser cleanly before we close the store.
    stop_ev = application.bot_data.get("summarizer_stop")
    sum_task = application.bot_data.get("summarizer_task")
    if stop_ev is not None:
        try:
            stop_ev.set()
        except Exception:
            pass
    if sum_task is not None:
        try:
            await asyncio.wait_for(sum_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass

    store: Store | None = application.bot_data.get("store")
    if store is not None:
        try:
            ids = await store.mark_running_as_interrupted()
            if ids:
                log.info(
                    "graceful shutdown: marked %d running tasks as interrupted: %s",
                    len(ids), ", ".join(ids),
                )
        except Exception as e:
            log.warning("graceful shutdown: could not mark interrupted: %s", e)

    # Give pending asyncio tasks a brief grace window. python-telegram-bot
    # cancels handler tasks first, but cascade subprocesses (claude_cli /
    # ollama) that are mid-flight need a moment to flush their last
    # progress events into the DB.
    pending = [
        task for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    ]
    if pending:
        log.info("graceful shutdown: waiting up to 30s for %d tasks", len(pending))
        try:
            await asyncio.wait(pending, timeout=30.0)
        except Exception as e:
            log.debug("graceful shutdown wait failed: %s", e)

    if store is not None:
        await store.close()
