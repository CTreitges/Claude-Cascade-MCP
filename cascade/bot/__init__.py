"""Claude-Cascade Telegram bot package.

Entry point: `python -m cascade.bot` or `from cascade.bot import main`.
The top-level `bot.py` re-exports everything for backwards compatibility
with existing tests and the systemd-service.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from cascade.config import settings

from .handlers.actions import on_action_callback
from .handlers.config import (
    cmd_effort,
    cmd_failsbeforereplan,
    cmd_iterations,
    cmd_lang,
    cmd_models,
    cmd_replan,
    cmd_repo,
    cmd_subtasks,
    cmd_toggles,
    on_effort_callback,
    on_failsbeforereplan_callback,
    on_iterations_callback,
    on_models_callback,
    on_replan_callback,
    on_subtasks_callback,
    on_toggles_callback,
)
from .handlers.general import (
    cmd_chat,
    cmd_errors,
    cmd_forget,
    cmd_help,
    cmd_settings,
    cmd_start,
    cmd_unknown,
    cmd_whoami,
)
from .handlers.messages import on_photo_or_document, on_text, on_voice
from .handlers.lifecycle_cmd import (
    cmd_restart,
    cmd_shutdown,
    on_lifecycle_callback,
)
from .handlers.setup import cmd_setup
from .handlers.skills import cmd_run_skill, cmd_skills, cmd_skillupgrade, on_skill_callback
from .handlers.system import cmd_exec, cmd_git, cmd_projects
from .handlers.tasks import (
    cmd_abort,
    cmd_wait,
    cmd_again,
    cmd_cancel,
    cmd_diff,
    cmd_dryrun,
    cmd_history,
    cmd_logs,
    cmd_queue,
    cmd_resume,
    cmd_status,
    cmd_stop,
)
from .lifecycle import post_init, post_shutdown

log = logging.getLogger("cascade.bot")


def main() -> None:
    from cascade.logging_config import setup_logging
    setup_logging()
    s = settings()
    if not s.telegram_bot_token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN not set.\n"
            "→ Run `cascade-setup` first (interactive wizard), or paste\n"
            "  the token into .env manually. The wizard writes to\n"
            "  secrets.env so your .env stays untouched.",
        )
    if not s.telegram_owner_id:
        log.warning(
            "TELEGRAM_OWNER_ID is unset — running in OPEN-CLAIM mode.\n"
            "The FIRST user who messages this bot becomes the owner and\n"
            "gets locked in. Send /start from your own Telegram account\n"
            "before anyone else does.",
        )

    app = (
        Application.builder()
        .token(s.telegram_bot_token)
        # CRITICAL: process updates concurrently. Without this PTB serializes
        # every update — while one `on_text` is busy with a 10-15 s triage
        # call, every other incoming message waits in the queue, and the bot
        # *appears* frozen during a running cascade. With concurrent_updates
        # each update gets its own asyncio.Task, so chat keeps flowing.
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # General
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("chat", cmd_chat))
    app.add_handler(CommandHandler("errors", cmd_errors))

    # Tasks
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("again", cmd_again))
    app.add_handler(CommandHandler("diff", cmd_diff))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("wait", cmd_wait))
    app.add_handler(CommandHandler("abort", cmd_abort))
    app.add_handler(CommandHandler("dryrun", cmd_dryrun))

    # Config
    app.add_handler(CommandHandler("repo", cmd_repo))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("models", cmd_models))
    app.add_handler(CommandHandler("effort", cmd_effort))
    app.add_handler(CommandHandler("replan", cmd_replan))
    app.add_handler(CommandHandler("iterations", cmd_iterations))
    app.add_handler(CommandHandler("failsbeforereplan", cmd_failsbeforereplan))
    app.add_handler(CommandHandler("subtasks", cmd_subtasks))
    app.add_handler(CommandHandler("toggles", cmd_toggles))

    # Skills
    app.add_handler(CommandHandler("skills", cmd_skills))
    app.add_handler(CommandHandler("run", cmd_run_skill))
    app.add_handler(CommandHandler("skillupgrade", cmd_skillupgrade))
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("shutdown", cmd_shutdown))
    app.add_handler(CommandHandler("restart", cmd_restart))

    # System
    app.add_handler(CommandHandler("exec", cmd_exec))
    app.add_handler(CommandHandler("git", cmd_git))
    app.add_handler(CommandHandler("projects", cmd_projects))

    # Callbacks
    app.add_handler(CallbackQueryHandler(on_models_callback, pattern=r"^m:"))
    app.add_handler(CallbackQueryHandler(on_effort_callback, pattern=r"^e:"))
    app.add_handler(CallbackQueryHandler(on_replan_callback, pattern=r"^r:"))
    app.add_handler(CallbackQueryHandler(on_iterations_callback, pattern=r"^i:"))
    app.add_handler(CallbackQueryHandler(on_failsbeforereplan_callback, pattern=r"^f:"))
    app.add_handler(CallbackQueryHandler(on_subtasks_callback, pattern=r"^st:"))
    app.add_handler(CallbackQueryHandler(on_toggles_callback, pattern=r"^tg:"))
    app.add_handler(CallbackQueryHandler(on_skill_callback, pattern=r"^sk:"))
    app.add_handler(CallbackQueryHandler(on_action_callback, pattern=r"^act:"))
    from .handlers.resume_kbd import on_resume_callback
    app.add_handler(CallbackQueryHandler(on_resume_callback, pattern=r"^resume:"))
    app.add_handler(CallbackQueryHandler(on_lifecycle_callback, pattern=r"^life:"))

    # Free-form messages
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, on_photo_or_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Catch-all for unknown /commands — must come last.
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))

    log.info("Cascade bot starting; owner=%s", s.telegram_owner_id)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
