"""Telegram bot — entry point.

The implementation lives in `cascade.bot` since v0.11 (split into modules
to keep each handler group browsable). This top-level `bot.py` is kept
for the systemd-service ExecStart and so existing tests / tools that
`import bot` continue to work.

Run:
    python bot.py
or:
    python -m cascade.bot
"""

from __future__ import annotations

# Re-export the helper surface that tests / other tools used to import
# directly from `bot.<name>`.
from cascade.bot import main
from cascade.bot.handlers.actions import on_action_callback
from cascade.bot.handlers.config import (
    cmd_effort,
    cmd_lang,
    cmd_models,
    cmd_replan,
    cmd_repo,
    effort_main_view as _effort_main_view,
    models_main_view as _models_main_view,
    on_effort_callback,
    on_models_callback,
    on_replan_callback,
)
from cascade.bot.handlers.general import (
    cmd_help,
    cmd_settings,
    cmd_start,
    cmd_unknown,
    cmd_whoami,
)
from cascade.bot.handlers.messages import on_photo_or_document, on_text, on_voice
from cascade.bot.handlers.skills import cmd_run_skill, cmd_skills, on_skill_callback
from cascade.bot.handlers.system import cmd_exec, cmd_git, cmd_projects
from cascade.bot.handlers.tasks import (
    cmd_abort,
    cmd_again,
    cmd_cancel,
    cmd_diff,
    cmd_dryrun,
    cmd_history,
    cmd_logs,
    cmd_queue,
    cmd_resume,
    cmd_status,
)
from cascade.bot.helpers import (
    fmt_local as _fmt_local,
    fmt_status_emoji as _fmt_status_emoji,
    format_progress_line as _format_progress_line,
    is_owner as _is_owner,
    lang_for as _lang,
    md_escape as _md_escape,
    owner_only as _owner_only,
    send as _send,
    send_long as _send_long,
)
from cascade.bot.lifecycle import post_init, post_shutdown
from cascade.bot.runner import run_task_for_chat as _run_task_for_chat
from cascade.bot.state import (
    EFFORT_LEVELS,
    GIT_WHITELIST,
    INFLIGHT as _INFLIGHT,
    LANG_OVERRIDE as _LANG_OVERRIDE,
    PENDING_SKILL as _PENDING_SKILL,
    REPLAN_CHOICES,
)
from cascade.config import settings  # tests monkeypatch bot.settings

__all__ = [
    "main",
    # legacy private helpers re-exported for tests
    "_fmt_local", "_fmt_status_emoji", "_format_progress_line",
    "_is_owner", "_owner_only", "_lang",
    "_md_escape", "_send", "_send_long",
    "_run_task_for_chat", "_models_main_view", "_effort_main_view",
    "_INFLIGHT", "_LANG_OVERRIDE", "_PENDING_SKILL",
    # commands & handlers (rarely accessed directly, but available)
    "cmd_help", "cmd_start", "cmd_whoami", "cmd_settings", "cmd_unknown",
    "cmd_status", "cmd_logs", "cmd_cancel", "cmd_history",
    "cmd_resume", "cmd_again", "cmd_diff", "cmd_queue", "cmd_abort", "cmd_dryrun",
    "cmd_repo", "cmd_lang", "cmd_models", "cmd_effort", "cmd_replan",
    "cmd_skills", "cmd_run_skill",
    "cmd_exec", "cmd_git", "cmd_projects",
    "on_text", "on_voice", "on_photo_or_document",
    "on_models_callback", "on_effort_callback", "on_replan_callback",
    "on_skill_callback", "on_action_callback",
    "post_init", "post_shutdown",
    "EFFORT_LEVELS", "REPLAN_CHOICES", "GIT_WHITELIST",
    "settings",
]


if __name__ == "__main__":
    main()
