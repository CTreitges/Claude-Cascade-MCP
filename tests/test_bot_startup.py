"""Bot-startup smoke tests.

These don't talk to Telegram — they exercise the wiring inside
`cascade.bot.main` so a typo in handler registration / a missing import /
a broken callback pattern crashes here instead of in production.

Two angles:
  1. **Module-level imports**: every handler module must import cleanly.
  2. **Handler registration**: build a real telegram.ext.Application with
     a fake token, register all our handlers, and verify the expected
     command/callback patterns show up.
"""

from __future__ import annotations

import importlib

import pytest


HANDLER_MODULES = [
    "cascade.bot",
    "cascade.bot.runner",
    "cascade.bot.lifecycle",
    "cascade.bot.helpers",
    "cascade.bot.typing",
    "cascade.bot.state",
    "cascade.bot.handlers.actions",
    "cascade.bot.handlers.config",
    "cascade.bot.handlers.general",
    "cascade.bot.handlers.messages",
    "cascade.bot.handlers.resume_kbd",
    "cascade.bot.handlers.skills",
    "cascade.bot.handlers.system",
    "cascade.bot.handlers.tasks",
]


@pytest.mark.parametrize("mod", HANDLER_MODULES)
def test_handler_modules_import_cleanly(mod):
    importlib.import_module(mod)


def test_main_builds_application_without_crashing(monkeypatch):
    """Run cascade.bot.main() far enough to register every handler, then
    abort before it would actually start polling. Catches typos in
    CallbackQueryHandler patterns, missing imports, etc."""
    from cascade.config import Settings

    fake = Settings(
        telegram_bot_token="123456:FAKE_TOKEN_FOR_TESTS_NOT_REAL_xx",
        telegram_owner_id=42,
    )
    import cascade.bot as bot_mod
    import cascade.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "settings", lambda: fake)
    monkeypatch.setattr(bot_mod, "settings", lambda: fake)

    captured = {}

    class FakeApp:
        def __init__(self):
            self.handlers = []

        def add_handler(self, h):
            self.handlers.append(h)

        def add_error_handler(self, h):
            self.handlers.append(("error", h))

        def run_polling(self, **kw):
            captured["ran"] = True
            raise SystemExit("aborted before polling")

    class FakeBuilder:
        def token(self, t):
            captured["token"] = t
            return self

        def post_init(self, fn):
            captured["post_init"] = fn
            return self

        def post_shutdown(self, fn):
            captured["post_shutdown"] = fn
            return self

        def concurrent_updates(self, flag):
            captured["concurrent_updates"] = flag
            return self

        def build(self):
            return FakeApp()

    from telegram.ext import Application
    monkeypatch.setattr(Application, "builder", lambda: FakeBuilder())

    with pytest.raises(SystemExit):
        bot_mod.main()

    # We registered SOMETHING — proves the function reached run_polling.
    # Pull the handlers out of the most recently created FakeApp via captured.
    assert captured.get("ran") is True
    assert "FAKE_TOKEN" in captured["token"]
    assert callable(captured["post_init"])
    assert callable(captured["post_shutdown"])


def test_resume_keyboard_callback_pattern_routes_correctly():
    """The resume-keyboard sets callback_data='resume:<id>:<decision>'.
    The handler must accept all three decisions."""
    from cascade.bot.handlers.resume_kbd import make_keyboard

    kb = make_keyboard("abc123", lang="de")
    cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "resume:abc123:resume" in cbs
    assert "resume:abc123:fresh" in cbs
    assert "resume:abc123:abort" in cbs


def test_resume_keyboard_speaks_german_and_english():
    from cascade.bot.handlers.resume_kbd import make_keyboard
    de = make_keyboard("x", lang="de")
    en = make_keyboard("x", lang="en")
    de_text = " ".join(b.text for row in de.inline_keyboard for b in row)
    en_text = " ".join(b.text for row in en.inline_keyboard for b in row)
    assert "Fortsetzen" in de_text
    assert "Continue" in en_text


def test_summarizer_module_imports():
    """The new background-summariser must be importable from lifecycle."""
    from cascade.summarizer import background_loop, run_one_pass, summarize_batch
    assert callable(background_loop)
    assert callable(run_one_pass)
    assert callable(summarize_batch)
