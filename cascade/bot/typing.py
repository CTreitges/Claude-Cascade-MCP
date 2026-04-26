"""Continuous Telegram "typing…" indicator.

Telegram's `sendChatAction(typing)` is shown for ~5 seconds, so we re-send
it every 4 seconds for as long as the bot is working — across all layers
(triage, cascade run, RLM lookups, …). Use it as an async context manager:

    async with TypingIndicator(ctx, chat_id):
        ...do work...

Errors during chat-action sends are swallowed: a missing typing indicator
must never break the user-facing flow.
"""

from __future__ import annotations

import asyncio
import logging

from telegram.constants import ChatAction
from telegram.ext import ContextTypes

log = logging.getLogger("cascade.bot.typing")


class TypingIndicator:
    """Async context manager that keeps the Telegram typing… animation alive
    until the wrapped block exits."""

    def __init__(
        self,
        ctx: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        *,
        # Telegram displays the "typing…" indicator for ~5s per send. 7s
        # gives a tiny natural pause between heartbeats and roughly halves
        # the API-call rate vs. the original 4s.
        interval_s: float = 7.0,
    ) -> None:
        self._ctx = ctx
        self._chat_id = chat_id
        self._interval = interval_s
        self._task: asyncio.Task | None = None

    async def _loop(self) -> None:
        try:
            while True:
                try:
                    await self._ctx.bot.send_chat_action(self._chat_id, ChatAction.TYPING)
                except Exception as e:
                    log.debug("send_chat_action failed: %s", e)
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            return

    async def __aenter__(self) -> "TypingIndicator":
        try:
            await self._ctx.bot.send_chat_action(self._chat_id, ChatAction.TYPING)
        except Exception as e:
            log.debug("initial send_chat_action failed: %s", e)
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
