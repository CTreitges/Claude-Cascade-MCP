"""Smoke tests for bot.py helpers — pure logic, no Telegram round-trip."""

from __future__ import annotations

from types import SimpleNamespace

import bot
from cascade.config import Settings


# ---------- _format_progress_line ----------


def test_progress_line_for_each_event():
    cases = [
        ("started", {}),
        ("planning", {}),
        ("planned", {"summary": "create hello.py"}),
        ("implementing", {"iteration": 1}),
        ("implemented", {"iteration": 1, "ops": 2, "failed": 0}),
        ("reviewing", {"iteration": 1}),
        ("reviewed", {"iteration": 1, "pass": True, "feedback": ""}),
        ("reviewed", {"iteration": 2, "pass": False, "feedback": "fix this"}),
        ("iteration_failed", {"iteration": 1, "feedback": "x"}),
        ("done", {"summary": "done"}),
        ("failed", {"summary": "broken"}),
        ("unknown_event", {}),
    ]
    for ev, payload in cases:
        result = bot._format_progress_line(ev, payload, "de")
        # Must never raise, always returns str|None
        assert result is None or isinstance(result, str)


def test_progress_line_lang_changes_text():
    de = bot._format_progress_line("implementing", {"iteration": 2}, "de")
    en = bot._format_progress_line("implementing", {"iteration": 2}, "en")
    assert de != en
    assert "Iteration 2" in de
    assert "iter 2" in en


def test_progress_line_reviewed_pass_vs_fail():
    p = bot._format_progress_line("reviewed", {"iteration": 1, "pass": True}, "de")
    f = bot._format_progress_line("reviewed", {"iteration": 1, "pass": False, "feedback": "fix"}, "de")
    assert "✅" in p
    assert "❌" in f
    assert "fix" in f


# ---------- _fmt_status_emoji ----------


def test_status_emojis_unique():
    statuses = ["pending", "running", "interrupted", "done", "failed", "cancelled"]
    emojis = {bot._fmt_status_emoji(s) for s in statuses}
    assert len(emojis) == len(statuses)


def test_unknown_status_returns_bullet():
    assert bot._fmt_status_emoji("xyz") == "•"


# ---------- _fmt_local timezone ----------


def test_fmt_local_uses_settings_tz(monkeypatch):
    """A summertime UTC instant rendered in Europe/Berlin must be 2h ahead of UTC."""
    from datetime import datetime, timezone
    ts = int(datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp())  # well into DST
    s = Settings(cascade_timezone="Europe/Berlin")
    import cascade.bot.helpers as _bh
    monkeypatch.setattr(_bh, "settings", lambda: s)
    out_berlin = bot._fmt_local(ts)
    s2 = Settings(cascade_timezone="UTC")
    monkeypatch.setattr(_bh, "settings", lambda: s2)
    out_utc = bot._fmt_local(ts)
    assert out_utc == "12:00:00"
    assert out_berlin == "14:00:00"  # CEST = UTC+2


def test_fmt_local_invalid_tz_falls_back(monkeypatch):
    s = Settings(cascade_timezone="Atlantis/Bermuda")
    import cascade.bot.helpers as _bh
    monkeypatch.setattr(_bh, "settings", lambda: s)
    # Should not crash; ZoneInfoNotFoundError is caught and tz=None used
    out = bot._fmt_local(1777127400)
    assert ":" in out  # some hh:mm:ss


# ---------- _is_owner / _owner_only ----------


def _fake_update(user_id: int | None) -> SimpleNamespace:
    """Build a minimal Update-like object."""
    user = SimpleNamespace(id=user_id) if user_id is not None else None
    return SimpleNamespace(effective_user=user, effective_chat=SimpleNamespace(id=999))


def test_is_owner_blocks_unset_owner_id(monkeypatch):
    s = Settings(telegram_owner_id=0)
    import cascade.bot.helpers as _bh
    monkeypatch.setattr(_bh, "settings", lambda: s)
    assert bot._is_owner(_fake_update(123)) is False


def test_is_owner_blocks_wrong_user(monkeypatch):
    s = Settings(telegram_owner_id=42)
    import cascade.bot.helpers as _bh
    monkeypatch.setattr(_bh, "settings", lambda: s)
    assert bot._is_owner(_fake_update(99)) is False


def test_is_owner_admits_correct_user(monkeypatch):
    s = Settings(telegram_owner_id=42)
    import cascade.bot.helpers as _bh
    monkeypatch.setattr(_bh, "settings", lambda: s)
    assert bot._is_owner(_fake_update(42)) is True


def test_is_owner_blocks_no_user(monkeypatch):
    s = Settings(telegram_owner_id=42)
    import cascade.bot.helpers as _bh
    monkeypatch.setattr(_bh, "settings", lambda: s)
    assert bot._is_owner(_fake_update(None)) is False


async def test_owner_only_returns_false_silently_for_others(monkeypatch):
    s = Settings(telegram_owner_id=42)
    import cascade.bot.helpers as _bh
    monkeypatch.setattr(_bh, "settings", lambda: s)
    assert await bot._owner_only(_fake_update(99), None) is False


async def test_owner_only_returns_true_for_owner(monkeypatch):
    s = Settings(telegram_owner_id=42)
    import cascade.bot.helpers as _bh
    monkeypatch.setattr(_bh, "settings", lambda: s)
    assert await bot._owner_only(_fake_update(42), None) is True


# ---------- _models_main_view / _effort_main_view ----------


def test_models_main_view_lists_workers():
    text, kb = bot._models_main_view("de", "opus", "qwen", "sonnet")
    assert "Planner" in text and "Implementer" in text and "Reviewer" in text
    assert "opus" in text and "qwen" in text and "sonnet" in text
    # Inline keyboard must include callback_data starting with m:w: for all 3 workers
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "m:w:planner" in callback_datas
    assert "m:w:implementer" in callback_datas
    assert "m:w:reviewer" in callback_datas
    assert "m:close" in callback_datas


def test_effort_main_view_lists_three_workers():
    text, kb = bot._effort_main_view("en", "high", "default", "low")
    assert "Planner" in text and "Reviewer" in text and "Triage" in text
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "e:w:planner" in callback_datas
    assert "e:w:reviewer" in callback_datas
    assert "e:w:triage" in callback_datas
    assert "e:close" in callback_datas


# ---------- _build_replan_feedback (lives in core.py) ----------


def test_build_replan_feedback_includes_qcs_and_iteration_history():
    from cascade.core import _build_replan_feedback
    from cascade.agents.planner import Plan
    from cascade.workspace import QualityCheck
    from cascade.store import Iteration

    plan = Plan(
        summary="x",
        steps=["s1"],
        files_to_touch=["a.py"],
        acceptance_criteria=["ac1"],
        quality_checks=[QualityCheck(name="bad", command="false", timeout_s=5)],
    )
    iters = [
        Iteration(n=0, implementer_output=None, reviewer_pass=None,
                  reviewer_feedback=None, diff_excerpt=None, created_at=0),
        Iteration(n=1, implementer_output=None, reviewer_pass=False,
                  reviewer_feedback="needs python3", diff_excerpt=None, created_at=0),
        Iteration(n=2, implementer_output=None, reviewer_pass=False,
                  reviewer_feedback="still broken", diff_excerpt=None, created_at=0),
    ]
    out = _build_replan_feedback(plan, iters)
    assert "PREVIOUS PLAN" in out
    assert "bad" in out  # quality check name
    assert "false" in out  # quality check command
    assert "needs python3" in out  # iteration feedback
    assert "still broken" in out
    # Iteration 0 (the plan itself) should NOT appear in iteration history
    assert "iter 0" not in out
