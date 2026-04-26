"""Human-in-the-loop helper: agents can pause and ask the user.

The cascade calls `await ask_user(...)`. That:

  1. Inserts a row into `chat_questions` (status: pending).
  2. Emits an `ask_user` progress event so the bot sends a Telegram
     message with the question text.
  3. Polls the row until it's been answered (the bot's free-form
     `on_text` handler intercepts the user's next message and stores
     it as the answer when there's a pending question).
  4. Returns the answer string.

Cancel / timeout: if the cancel_event fires or `timeout_s` elapses,
the question is marked expired and a sensible fallback is returned.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger("cascade.feedback")


async def ask_user(
    store: "Store",
    chat_id: int,
    question: str,
    *,
    task_id: str | None = None,
    progress: Callable[..., Awaitable] | None = None,
    cancel_event: asyncio.Event | None = None,
    timeout_s: float = 30 * 60,  # 30 min default before giving up
    poll_interval_s: float = 1.5,
    fallback: str = "",
) -> str:
    """Ask the user a free-form question via Telegram. Returns the answer
    string (or `fallback` on timeout/cancel)."""
    if not chat_id:
        return fallback
    qid = await store.create_chat_question(chat_id, question, task_id=task_id)
    log.info("ask_user qid=%d chat=%s task=%s: %s", qid, chat_id, task_id, question[:120])
    if progress is not None:
        try:
            await progress(task_id or "?", "ask_user", {"question": question, "qid": qid})
        except Exception:
            pass

    waited = 0.0
    # Check-then-sleep so a fast answer doesn't have to wait for the
    # first poll interval. Also lets tests with tiny timeouts work.
    while True:
        if cancel_event is not None and cancel_event.is_set():
            await store.expire_chat_question(qid)
            return fallback
        row = await store.get_question(qid)
        if row and row.get("answered_at"):
            return row.get("answer") or fallback
        if waited >= timeout_s:
            await store.expire_chat_question(qid)
            log.warning("ask_user qid=%d timed out after %.0fs", qid, timeout_s)
            return fallback
        sleep_for = min(poll_interval_s, max(0.05, timeout_s - waited))
        await asyncio.sleep(sleep_for)
        waited += sleep_for
