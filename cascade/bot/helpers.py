"""Reusable bot-side helpers: owner gate, formatting, message helpers."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import Message, Update
from telegram.constants import ParseMode

from cascade.config import settings
from cascade.i18n import t  # noqa: F401 — re-exported for handler convenience

from .state import LANG_OVERRIDE

log = logging.getLogger("cascade.bot.helpers")


def lang_for(update: Update) -> str:
    if update.effective_chat and update.effective_chat.id in LANG_OVERRIDE:
        return LANG_OVERRIDE[update.effective_chat.id]
    return settings().cascade_bot_lang


def local_tz():
    try:
        return ZoneInfo(settings().cascade_timezone)
    except ZoneInfoNotFoundError:
        return None


def fmt_local(ts: float, fmt: str = "%H:%M:%S") -> str:
    tz = local_tz()
    return datetime.fromtimestamp(ts, tz=tz).strftime(fmt)


def fmt_status_emoji(status: str) -> str:
    return {
        "pending": "⏳",
        "running": "🔁",
        "interrupted": "⏸",
        "done": "✅",
        "failed": "❌",
        "cancelled": "🚫",
    }.get(status, "•")


def is_owner(update: Update) -> bool:
    s = settings()
    user = update.effective_user
    return bool(user and s.telegram_owner_id and user.id == s.telegram_owner_id)


# Module-level guard so the auto-claim happens exactly once even under
# concurrent updates. Set when the file write succeeds.
_owner_claimed_in_process = False


async def owner_only(update: Update, _ctx) -> bool:
    """Gate every handler against the configured owner.

    First-run claim flow:  if `TELEGRAM_OWNER_ID` is unset (= 0) AND no
    one has claimed yet in this process, accept the very first sender,
    persist their id into `secrets.env`, and update the Settings cache
    in-process so subsequent updates are gated normally. Logs a clear
    `OWNER CLAIMED` warning so operators see what happened — important
    because the *first message wins* model means whoever messages a
    freshly-deployed bot first becomes its owner.
    """
    global _owner_claimed_in_process
    s = settings()
    user = update.effective_user
    if user is None:
        return False

    if s.telegram_owner_id == 0 and not _owner_claimed_in_process:
        # Auto-claim
        try:
            from cascade.secrets_store import set_secret
            set_secret("TELEGRAM_OWNER_ID", str(user.id))
            # Also patch the in-process Settings so further calls don't
            # re-trigger the claim path (Settings is cached).
            s.telegram_owner_id = user.id  # type: ignore[misc]
            _owner_claimed_in_process = True
            log.warning(
                "OWNER CLAIMED — telegram_owner_id auto-set to %s "
                "(@%s, name=%s) on first incoming message",
                user.id, user.username or "?", user.first_name or "?",
            )
            try:
                await update.effective_message.reply_text(
                    f"✅ Owner gesetzt: `{user.id}` (@{user.username or '?'})\n"
                    f"Ich bin jetzt fest auf dieses Konto gelockt. "
                    f"`/setup` fügt API-Keys hinzu — `/help` zeigt alles.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass
            return True
        except Exception as e:
            log.error("owner auto-claim failed: %s", e)

    if not is_owner(update):
        log.warning(
            "ignored unauthorized user id=%s",
            getattr(update.effective_user, "id", "?"),
        )
        return False
    return True


async def send(message: Message, text: str, *, code: bool = False) -> Message:
    if code:
        text = f"```\n{text[:3900]}\n```"
        return await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    return await message.reply_text(text[:4000])


def md_escape(s: str) -> str:
    """Escape characters that would break Telegram Markdown parsing."""
    if not s:
        return ""
    for ch in ("_", "*", "`", "["):
        s = s.replace(ch, "\\" + ch)
    return s


async def send_long(message: Message, text: str, *, code: bool = False, chunk: int = 3500) -> None:
    """Send a long string as multiple messages, optionally each in a code block."""
    if not text:
        return
    pieces = [text[i:i + chunk] for i in range(0, len(text), chunk)]
    for i, piece in enumerate(pieces):
        prefix = "" if len(pieces) == 1 else f"({i + 1}/{len(pieces)})\n"
        if code:
            await message.reply_text(f"{prefix}```\n{piece}\n```", parse_mode=ParseMode.MARKDOWN)
        else:
            await message.reply_text(prefix + piece)


def format_progress_line(event: str, payload: dict, lang: str = "de") -> str | None:
    # Sub-task prefix for decomposed runs: makes the live status message
    # show "🪓 'core-modules' · iter 1: implementing" instead of just
    # "iter 1: implementing", so users can track which slice is in flight.
    sub = payload.get("subtask")
    sub_prefix = f"🪓 `{sub}` · " if sub else ""

    if event == "started":
        return ""
    if event == "planning":
        return t("progress.planning", lang=lang)
    if event == "planned":
        return t("progress.planned", lang=lang, summary=(payload.get("summary") or "")[:120])
    if event == "log":
        msg = payload.get("msg", "")
        return f"  ▸ {msg}" if msg else None
    if event == "implementing":
        return sub_prefix + t("progress.implementing", lang=lang, n=payload.get("iteration"))
    if event == "implemented":
        return sub_prefix + t(
            "progress.implemented",
            lang=lang,
            n=payload.get("iteration"),
            ops=payload.get("ops", 0),
            failed=payload.get("failed", 0),
        )
    if event == "reviewing":
        return sub_prefix + t("progress.reviewing", lang=lang, n=payload.get("iteration"))
    if event == "reviewed":
        fb = (payload.get("feedback") or "").strip().splitlines()[0:1]
        suffix = f": {fb[0][:120]}" if fb else ""
        key = "progress.reviewed_pass" if payload.get("pass") else "progress.reviewed_fail"
        return sub_prefix + t(key, lang=lang, n=payload.get("iteration"), suffix=suffix)
    if event in ("iteration_failed", "done"):
        return ""
    if event == "failed":
        return t("progress.failed", lang=lang, summary=payload.get("summary", ""))
    return None
