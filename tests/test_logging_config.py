from __future__ import annotations

import logging
from pathlib import Path

import pytest

from cascade.config import Settings


@pytest.fixture
def isolated_settings(tmp_path: Path, monkeypatch):
    fake = Settings(cascade_home=tmp_path, cascade_debug=False)
    import cascade.config as cfg_mod
    import cascade.logging_config as log_mod
    monkeypatch.setattr(cfg_mod, "settings", lambda: fake)
    monkeypatch.setattr(log_mod, "settings", lambda: fake)
    return fake


def test_setup_logging_idempotent(isolated_settings: Settings):
    from cascade.logging_config import setup_logging

    setup_logging(debug=False)
    n1 = len([h for h in logging.getLogger().handlers
              if getattr(h, "_cascade_owned", False)])
    setup_logging(debug=False)
    n2 = len([h for h in logging.getLogger().handlers
              if getattr(h, "_cascade_owned", False)])
    # Re-init must NOT stack handlers — exactly one console handler each time.
    assert n1 == 1
    assert n2 == 1


def test_setup_logging_debug_creates_rotating_file(
    isolated_settings: Settings, tmp_path: Path,
):
    from cascade.logging_config import setup_logging

    setup_logging(debug=True)
    handlers = [h for h in logging.getLogger().handlers
                if getattr(h, "_cascade_owned", False)]
    # Console + rotating file
    assert len(handlers) == 2
    # debug.log must be created on first write
    logging.getLogger("cascade").debug("first debug line")
    debug_log = tmp_path / "store" / "debug.log"
    assert debug_log.exists()
    assert "first debug line" in debug_log.read_text()


def test_setup_logging_no_debug_no_file(
    isolated_settings: Settings, tmp_path: Path,
):
    from cascade.logging_config import setup_logging

    setup_logging(debug=False)
    debug_log = tmp_path / "store" / "debug.log"
    # Without debug mode the rotating debug.log handler should NOT be wired.
    assert not debug_log.exists()
    handlers = [h for h in logging.getLogger().handlers
                if getattr(h, "_cascade_owned", False)]
    assert len(handlers) == 1  # only console


def test_audit_telegram_writes_to_telegram_log(
    isolated_settings: Settings, tmp_path: Path,
):
    from cascade.logging_config import audit_telegram, setup_logging

    setup_logging(debug=False)
    audit_telegram(123456, "text", text_len=42, has_attachment=False)
    audit_telegram(123456, "document", text_len=10, has_attachment=True)

    # Force flush of audit handler so the file is on disk.
    for h in logging.getLogger("cascade.audit.telegram").handlers:
        h.flush()

    audit_log = tmp_path / "store" / "telegram.log"
    assert audit_log.exists()
    text = audit_log.read_text()
    assert "chat_id=123456" in text
    assert "kind=text" in text
    assert "kind=document" in text
    assert "attached=1" in text


def test_audit_telegram_does_not_propagate_to_console(
    isolated_settings: Settings, caplog: pytest.LogCaptureFixture,
):
    from cascade.logging_config import audit_telegram, setup_logging

    setup_logging(debug=False)
    with caplog.at_level(logging.INFO):
        audit_telegram(7, "text", text_len=5)
    # The audit logger uses propagate=False — must not show up in root caplog
    # at INFO level for chat_id=7.
    audit_lines = [r for r in caplog.records if "chat_id=7" in r.getMessage()]
    assert audit_lines == []


def test_setup_logging_quiets_httpx(isolated_settings: Settings):
    from cascade.logging_config import setup_logging

    setup_logging(debug=False)
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_setup_logging_reads_debug_from_settings(
    tmp_path: Path, monkeypatch,
):
    from cascade.config import Settings
    fake = Settings(cascade_home=tmp_path, cascade_debug=True)
    import cascade.logging_config as log_mod
    monkeypatch.setattr(log_mod, "settings", lambda: fake)
    log_mod.setup_logging()  # debug=None → reads settings
    handlers = [h for h in logging.getLogger().handlers
                if getattr(h, "_cascade_owned", False)]
    assert any(h.__class__.__name__ == "RotatingFileHandler" for h in handlers)
