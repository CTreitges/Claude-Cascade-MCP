"""Telegram commands `/shutdown` and `/restart`.

Both call `systemctl --user <stop|restart> cascade-bot` from a *detached*
subprocess so the action survives the bot killing itself. Running tasks
are gracefully marked `interrupted` by the existing `post_shutdown`
hook before the process exits.

Why detached subprocess: when the bot calls `systemctl restart cascade-bot`
on itself, systemd sends SIGTERM. If we invoked systemctl in-process,
the bash subprocess would die together with the parent before sending
the actual restart command. Using `start_new_session=True` puts the
helper into its own process group so it outlives us.

Safety:
  - Both commands require explicit confirmation. Either:
    * an inline keyboard tap, or
    * a second-message `confirm` argument (`/shutdown confirm`).
  - The bot warns about any in-flight tasks first — they will be
    `interrupted` in the DB; with CASCADE_AUTO_RESUME_INTERRUPTED=true
    they'll auto-pick-up; otherwise `/resume <id>` after restart.
  - `/shutdown` is one-way: from Telegram you cannot restart, only
    physical shell access can bring the bot back up.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import uuid
from typing import Literal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode

from cascade.store import Store

from ..helpers import lang_for, owner_only
from ..state import INFLIGHT

log = logging.getLogger("cascade.bot.handlers.lifecycle_cmd")


# Pending shutdown/restart requests: callback_id → action
_PENDING_LIFECYCLE: dict[str, Literal["shutdown", "restart"]] = {}


async def _running_summary(store: Store, lang: str) -> str:
    """Brief list of in-flight tasks. Empty string if nothing's running."""
    if not INFLIGHT:
        return ""
    lines = []
    for cid, (tid, _task, _ev) in INFLIGHT.items():
        try:
            t = await store.get_task(tid)
            label = (t.task_text or "")[:80] if t else "?"
        except Exception:
            label = "?"
        lines.append(f"  • `{tid}` (chat `{cid}`) — {label}")
    head = (
        f"⚠️ {len(INFLIGHT)} laufende Task(s) werden als `interrupted` markiert:"
        if lang == "de"
        else f"⚠️ {len(INFLIGHT)} running task(s) will be marked `interrupted`:"
    )
    return head + "\n" + "\n".join(lines)


def _confirm_keyboard(callback_id: str, action: str, lang: str) -> InlineKeyboardMarkup:
    if lang == "de":
        ok = "✅ Bestätigen"
        cancel = "❌ Abbrechen"
    else:
        ok = "✅ Confirm"
        cancel = "❌ Cancel"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(ok,     callback_data=f"life:{callback_id}:{action}:go"),
        InlineKeyboardButton(cancel, callback_data=f"life:{callback_id}:{action}:no"),
    ]])


async def _ask_confirmation(
    update: Update, ctx, action: Literal["shutdown", "restart"],
) -> None:
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    msg = update.effective_message

    # Argument-based fast-path: `/shutdown confirm` or `/restart confirm`
    args = ctx.args or []
    if args and args[0].lower() in ("confirm", "go", "ja", "yes"):
        await _execute(update, ctx, action)
        return

    inflight_block = await _running_summary(store, lang)
    if action == "shutdown":
        head = (
            "🛑 *Bot herunterfahren?*\n"
            "Nach `/shutdown` kann der Bot NUR per Shell-Zugriff wieder "
            "gestartet werden — nicht via Telegram."
        ) if lang == "de" else (
            "🛑 *Stop the bot?*\n"
            "After `/shutdown` only shell access can bring it back up — "
            "no Telegram restart possible."
        )
    else:  # restart
        head = (
            "🔁 *Bot neustarten?*\n"
            "Der systemd-User-Service wird neugestartet. In-flight Tasks "
            "werden `interrupted` markiert."
        ) if lang == "de" else (
            "🔁 *Restart the bot?*\n"
            "Restarts the systemd user-service. In-flight tasks will be "
            "marked `interrupted`."
        )
    body = head
    if inflight_block:
        body += "\n\n" + inflight_block

    cb_id = uuid.uuid4().hex[:12]
    _PENDING_LIFECYCLE[cb_id] = action
    await msg.reply_text(
        body,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_confirm_keyboard(cb_id, action, lang),
    )


async def cmd_shutdown(update: Update, ctx) -> None:
    """Stop the bot's systemd-user-service. Requires explicit confirmation."""
    if not await owner_only(update, ctx):
        return
    await _ask_confirmation(update, ctx, "shutdown")


async def cmd_restart(update: Update, ctx) -> None:
    """Restart the bot's systemd-user-service. Survives in-flight tasks
    (they'll be `interrupted` and resumable via /resume <id>)."""
    if not await owner_only(update, ctx):
        return
    await _ask_confirmation(update, ctx, "restart")


async def _execute(
    update: Update, ctx, action: Literal["shutdown", "restart"],
) -> None:
    """Spawn a detached `systemctl --user <stop|restart> cascade-bot` so the
    helper process outlives the bot's own SIGTERM."""
    lang = lang_for(update)
    msg = update.effective_message
    verb = (
        ("Herunterfahren" if action == "shutdown" else "Neustart")
        if lang == "de"
        else ("Shutdown" if action == "shutdown" else "Restart")
    )
    try:
        await msg.reply_text(
            f"🔧 {verb} läuft … (in 1s)" if lang == "de" else f"🔧 {verb} in 1s …",
        )
    except Exception:
        pass

    cmd = ["systemctl", "--user", "stop" if action == "shutdown" else "restart", "cascade-bot"]
    # `nohup … &` via shell ensures the child detaches cleanly even if
    # the parent (us) gets SIGTERM during the same systemctl call.
    shell_cmd = (
        "sleep 1; " + " ".join(f"'{p}'" for p in cmd) + " </dev/null >/dev/null 2>&1"
    )
    try:
        subprocess.Popen(
            ["bash", "-c", shell_cmd],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.warning("%s requested via Telegram by user %s", action, update.effective_user.id)
    except Exception as e:
        log.error("could not spawn systemctl helper: %s", e)
        try:
            await msg.reply_text(
                f"❌ Fehler beim {verb}: {e}\nBitte per Shell prüfen."
                if lang == "de"
                else f"❌ {verb} failed: {e}\nCheck via shell.",
            )
        except Exception:
            pass


async def on_lifecycle_callback(update: Update, ctx) -> None:
    """Inline-keyboard router for `life:<cb_id>:<action>:<go|no>`."""
    if not await owner_only(update, ctx):
        return
    q = update.callback_query
    if not q or not q.data:
        return
    parts = q.data.split(":", 3)
    if len(parts) != 4 or parts[0] != "life":
        return
    cb_id, action, decision = parts[1], parts[2], parts[3]
    pending = _PENDING_LIFECYCLE.pop(cb_id, None)
    if pending is None or pending != action:
        await q.answer("(abgelaufen)" if lang_for(update) == "de" else "(expired)")
        return
    lang = lang_for(update)
    if decision == "no":
        await q.answer("Abgebrochen" if lang == "de" else "Cancelled")
        try:
            await q.edit_message_reply_markup(reply_markup=None)
            await q.edit_message_text(
                "🚫 Abgebrochen." if lang == "de" else "🚫 Cancelled.",
            )
        except Exception:
            pass
        return
    # decision == "go"
    await q.answer()
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    # Make `update.effective_message` usable for _execute's reply_text
    # (callback queries have a message we can reply on).
    await _execute(update, ctx, action)  # type: ignore[arg-type]
    # Give the message a brief moment to flush before systemctl arrives.
    await asyncio.sleep(0.2)
