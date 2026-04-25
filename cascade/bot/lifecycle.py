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


async def post_shutdown(application: Application) -> None:
    store: Store | None = application.bot_data.get("store")
    if store is not None:
        await store.close()
