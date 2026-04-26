"""Centralized logging setup for the bot, MCP-server and CLI entrypoints.

Three sinks:

  1. **Console (stderr)** — INFO by default, DEBUG when `cascade_debug=True`
     (or `CASCADE_DEBUG=1` in `.env`). Format includes wall-clock time,
     level, logger name (padded), and message — readable in
     `journalctl --user -u cascade-bot -f`.

  2. **store/debug.log (rotating)** — only created when debug mode is on.
     RotatingFileHandler 10MB × 5 — captures every cascade.* logger at
     DEBUG. Useful for post-mortems where the journal has rolled.

  3. **store/telegram.log (rotating)** — one line per Telegram update
     (chat_id, kind, text-len, has-attachment). Always on; tiny volume.
     Written via the dedicated logger `cascade.audit.telegram`. Code that
     wants to emit there imports `audit_telegram` and calls it.

Tame the noisy libraries:

  - `httpx` / `httpcore` are otherwise INFO-level chatty (every typing
    indicator beat → `sendChatAction` → 200 line in journal). Pinned to
    WARNING.
  - `telegram.ext` and `telegram.bot` keep their level (they emit useful
    INFO occasionally — e.g. polling restarts).

Idempotent: safe to call multiple times, the second call just refreshes
levels / replaces the rotating handler.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable

from .config import settings


_CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(name)-25s | %(message)s"
_FILE_FORMAT = (
    "%(asctime)s %(levelname)-7s %(name)-25s "
    "[%(filename)s:%(lineno)d] | %(message)s"
)

_AUDIT_TELEGRAM_NAME = "cascade.audit.telegram"

# Loggers we always quiet down regardless of debug mode.
_NOISY_THIRD_PARTY: dict[str, int] = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    # Telegram BotAPI request logs are also INFO-level chatty during typing
    # storms; keep at WARNING unless someone explicitly debugs the lib.
    "telegram.request": logging.WARNING,
}

# Loggers that should ALWAYS get DEBUG when debug mode is on (full firehose).
_CASCADE_LOGGERS: Iterable[str] = (
    "cascade",
    "cascade.bot",
    "cascade.bot.messages",
    "cascade.bot.runner",
    "cascade.triage",
    "cascade.memory",
    "cascade.chat_memory",
    "cascade.healing",
    "cascade.agents",
    "cascade.runner",
    "cascade.error_log",
    "cascade.rate_limit",
    "cascade.research",
)


def setup_logging(*, debug: bool | None = None) -> None:
    """Initialize console + file handlers. `debug=None` reads `cascade_debug`
    from settings (which honours `CASCADE_DEBUG` in `.env`).

    Idempotent — calling twice replaces the rotating handler instead of
    stacking duplicates.
    """
    if debug is None:
        try:
            debug = bool(settings().cascade_debug)
        except Exception:
            debug = os.getenv("CASCADE_DEBUG", "").lower() in ("1", "true", "yes")

    root = logging.getLogger()
    # Wipe any previous handlers we own (idempotent re-config).
    for h in list(root.handlers):
        if getattr(h, "_cascade_owned", False):
            root.removeHandler(h)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if debug else logging.INFO)
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
    console._cascade_owned = True  # type: ignore[attr-defined]
    root.addHandler(console)
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    for name in _CASCADE_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG if debug else logging.INFO)
    for name, lvl in _NOISY_THIRD_PARTY.items():
        logging.getLogger(name).setLevel(lvl)

    # store/debug.log — only when debug=True
    if debug:
        try:
            path = _store_path("debug.log")
            handler = RotatingFileHandler(
                path, maxBytes=10 * 1024 * 1024, backupCount=5,
                encoding="utf-8",
            )
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(logging.Formatter(_FILE_FORMAT))
            handler._cascade_owned = True  # type: ignore[attr-defined]
            root.addHandler(handler)
        except Exception as e:
            logging.getLogger("cascade").warning(
                "could not open debug.log: %s", e,
            )

    # store/telegram.log — always, lightweight audit trail. Re-init: drop any
    # previous audit handlers so a path change (e.g. tmp_path in tests, or a
    # CASCADE_HOME swap) propagates instead of silently writing to the old
    # location.
    audit = logging.getLogger(_AUDIT_TELEGRAM_NAME)
    for h in list(audit.handlers):
        if getattr(h, "_cascade_audit", False):
            audit.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
    try:
        path = _store_path("telegram.log")
        ah = RotatingFileHandler(
            path, maxBytes=5 * 1024 * 1024, backupCount=3,
            encoding="utf-8",
        )
        ah.setLevel(logging.INFO)
        ah.setFormatter(logging.Formatter(
            "%(asctime)s %(message)s",
        ))
        ah._cascade_audit = True  # type: ignore[attr-defined]
        audit.addHandler(ah)
        audit.setLevel(logging.INFO)
        audit.propagate = False  # don't echo into the console firehose
    except Exception as e:
        logging.getLogger("cascade").warning(
            "could not open telegram.log: %s", e,
        )

    logging.getLogger("cascade").info(
        "logging initialised — debug=%s, console=%s, telegram_audit=%s",
        debug,
        "DEBUG" if debug else "INFO",
        any(getattr(h, "_cascade_audit", False) for h in audit.handlers),
    )


def audit_telegram(
    chat_id: int,
    kind: str,
    *,
    text_len: int = 0,
    has_attachment: bool = False,
    extra: str | None = None,
) -> None:
    """Append one line to store/telegram.log. Never raises.

    `kind` is a short stable string: 'text', 'voice', 'photo', 'document',
    'callback_query', 'pending_question_answer', etc.
    """
    try:
        bits = [
            f"chat_id={chat_id}",
            f"kind={kind}",
            f"text_len={text_len}",
        ]
        if has_attachment:
            bits.append("attached=1")
        if extra:
            bits.append(extra)
        logging.getLogger(_AUDIT_TELEGRAM_NAME).info(" ".join(bits))
    except Exception:
        pass


def _store_path(name: str) -> Path:
    p = settings().cascade_home / "store" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
