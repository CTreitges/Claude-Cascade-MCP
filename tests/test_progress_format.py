"""Tests for cascade.progress_format — the shared milestone-line
formatter used by both the Telegram bot and the /cascade slash-command
in Claude Code.
"""

from __future__ import annotations

from cascade.progress_format import format_milestone, parse_log_message


# ---- format_milestone ---------------------------------------------------


def test_unknown_event_returns_empty():
    assert format_milestone("started", {}) == []
    assert format_milestone("planning", {}) == []
    assert format_milestone("implementing", {"iteration": 1}) == []
    assert format_milestone("nonsense", {}) == []


def test_planned_renders_summary_and_subtask_list_de():
    lines = format_milestone(
        "planned",
        {
            "summary": "Build a CLI tool",
            "steps": ["s1", "s2", "s3"],
            "subtasks": ["cli-skeleton", "tests", "docs"],
        },
        lang="de",
    )
    assert any("Plan steht" in ln for ln in lines)
    assert any("3 Steps" in ln for ln in lines)
    assert any("3 Sub-Tasks" in ln for ln in lines)
    assert any("Build a CLI tool" in ln for ln in lines)
    assert any("cli-skeleton" in ln for ln in lines)


def test_planned_renders_english():
    lines = format_milestone(
        "planned",
        {"summary": "ok", "steps": ["a"], "subtasks": []},
        lang="en",
    )
    assert any("Plan ready" in ln for ln in lines)


def test_planned_truncates_long_summary():
    lines = format_milestone(
        "planned",
        {"summary": "x" * 1000, "steps": [], "subtasks": []},
        lang="en",
    )
    # Summary line should be capped
    summary_line = next((ln for ln in lines if "x" in ln), "")
    assert len(summary_line) < 300


def test_implemented_with_failed_ops():
    lines = format_milestone(
        "implemented",
        {"iteration": 3, "ops": 5, "failed": 2, "subtask": "tests"},
    )
    assert lines == ["🔧 iter 3 — 5 ops, 2 failed [tests]"]


def test_implemented_clean():
    lines = format_milestone(
        "implemented", {"iteration": 1, "ops": 4, "failed": 0},
    )
    assert lines == ["🔧 iter 1 — 4 ops"]


def test_reviewed_pass_subtask_de_uses_german():
    lines = format_milestone(
        "reviewed",
        {"iteration": 4, "pass": True, "subtask": "cli-skeleton"},
        lang="de",
    )
    assert lines == ["✅ Sub-Task cli-skeleton abgeschlossen (Iter 4)"]


def test_reviewed_pass_subtask_en():
    lines = format_milestone(
        "reviewed",
        {"iteration": 4, "pass": True, "subtask": "tests"},
        lang="en",
    )
    assert lines == ["✅ Sub-task tests complete (iter 4)"]


def test_reviewed_pass_no_subtask():
    lines = format_milestone(
        "reviewed", {"iteration": 2, "pass": True}, lang="en",
    )
    assert lines == ["✅ iter 2 review pass"]


def test_reviewed_fail_includes_first_feedback_line():
    lines = format_milestone(
        "reviewed",
        {
            "iteration": 5,
            "pass": False,
            "feedback": "Needs python3 instead of python\nAlso missing tests",
        },
        lang="en",
    )
    assert lines[0] == "❌ iter 5 review fail"
    assert "Needs python3" in lines[1]
    # Multi-line feedback collapses to first line only
    assert "missing tests" not in " ".join(lines)


def test_log_subtask_message():
    lines = format_milestone("log", {"msg": "subtask 2/4: tests"})
    assert lines == ["🪓 subtask 2/4: tests"]


def test_log_stuck_alert():
    lines = format_milestone(
        "log", {"msg": "60s idle", "kind": "stuck-alert"},
    )
    assert lines[0].startswith("⚠️")


def test_log_permission_issue():
    lines = format_milestone(
        "log",
        {"msg": "permission-denied detected", "kind": "permission-issue"},
    )
    assert lines[0].startswith("🔒")


def test_log_implementer_stuck():
    lines = format_milestone(
        "log", {"msg": "3 identical outputs", "kind": "implementer-stuck"},
    )
    assert lines[0].startswith("🔁")


def test_log_unrelated_message_filtered():
    """Generic info-log lines (no matching kind/msg) are not surfaced."""
    assert format_milestone("log", {"msg": "random info"}) == []


def test_replanning_includes_counter():
    lines = format_milestone(
        "replanning",
        {"after_iteration": 4, "replans_done": 1},
        lang="en",
    )
    assert lines[0] == "🔄 Replanning #2 after iter 4…"


def test_replanned_with_checks():
    lines = format_milestone(
        "replanned",
        {
            "summary": "Use python3 explicitly in checks",
            "checks": ["py-compile", "ruff", "pytest"],
        },
        lang="en",
    )
    assert any("New plan" in ln for ln in lines)
    assert any("py-compile" in ln for ln in lines)


def test_iteration_failed_with_feedback():
    lines = format_milestone(
        "iteration_failed",
        {"iteration": 3, "feedback": "First problem\nSecond detail"},
        lang="en",
    )
    assert lines[0] == "❌ iter 3 failed"
    assert "First problem" in lines[1]
    assert "Second detail" not in " ".join(lines)


def test_waiting_for_session_formats_long_wait():
    lines = format_milestone(
        "waiting_for_session",
        {"seconds": 3 * 86400 + 14 * 3600, "attempt": 1, "reason": "weekly cap"},
        lang="en",
    )
    assert any("3d 14h" in ln for ln in lines)
    assert any("weekly cap" in ln for ln in lines)


def test_waiting_for_session_short_wait():
    lines = format_milestone(
        "waiting_for_session",
        {"seconds": 90, "attempt": 1, "reason": ""},
        lang="en",
    )
    assert any("1min 30s" in ln for ln in lines)


def test_skill_suggested():
    lines = format_milestone(
        "skill_suggested",
        {"name": "drop_credentials", "description": "Stage a JSON credential file"},
    )
    assert lines[0] == "💡 Skill suggestion: drop_credentials"
    assert "Stage a JSON credential file" in lines[1]


def test_done_event():
    lines = format_milestone(
        "done", {"summary": "all green"}, lang="en",
    )
    assert lines == ["✅ Done — all green"]


def test_failed_event():
    lines = format_milestone(
        "failed",
        {"reason": "stagnation_replan_exhausted", "feedback": "still broken"},
        lang="en",
    )
    assert lines[0].startswith("❌")
    assert any("stagnation" in ln for ln in lines)


def test_cancelled():
    lines = format_milestone("cancelled", {}, lang="en")
    assert lines == ["🚫 Cancelled"]


def test_format_never_raises_on_garbage_payload():
    """Defensively coerces None / non-dict payloads to {} — never crashes.
    May still produce a fallback line (e.g. 'Plan steht — 0 Steps') —
    that's preferable to silent loss of an event."""
    out_none = format_milestone("planned", None)  # type: ignore[arg-type]
    out_str = format_milestone("planned", "not-a-dict")  # type: ignore[arg-type]
    out_wrong = format_milestone("planned", {"steps": "wrong-type"})
    # Important: the function returns (no exception). Output may be a
    # short fallback line — we just don't want a crash here.
    assert isinstance(out_none, list)
    assert isinstance(out_str, list)
    assert isinstance(out_wrong, list)


# ---- parse_log_message --------------------------------------------------


def test_parse_log_recovers_event_and_payload():
    msg = 'planned: {"summary": "hi", "steps": ["a"]}'
    out = parse_log_message(msg)
    assert out is not None
    event, payload = out
    assert event == "planned"
    assert payload["summary"] == "hi"


def test_parse_log_handles_truncated_json():
    # Simulate _emit's 300-char truncation cutting JSON mid-string.
    msg = 'planned: {"summary": "very long st'
    out = parse_log_message(msg)
    # Should still return event with empty payload — we'd rather show
    # a one-line "planned" milestone than skip it entirely.
    assert out is not None
    assert out[0] == "planned"


def test_parse_log_skips_non_event_lines():
    """Raw _log() messages without an event prefix are skipped."""
    assert parse_log_message("resume: iteration 0 plan was corrupt") is None
    assert parse_log_message("recall: foo bar") is None
    assert parse_log_message("just text") is None
    assert parse_log_message("") is None


def test_parse_log_skips_arrays_at_top_level():
    """Only dict-shaped JSON payloads are accepted."""
    msg = 'planned: [1, 2, 3]'
    assert parse_log_message(msg) is None


# ---- Round-trip through the formatter ----------------------------------


def test_round_trip_planned_event():
    msg = (
        'planned: {"summary": "build", "steps": ["s1", "s2"], '
        '"subtasks": ["a", "b"]}'
    )
    parsed = parse_log_message(msg)
    assert parsed is not None
    event, payload = parsed
    lines = format_milestone(event, payload, lang="de")
    assert any("Plan steht" in ln for ln in lines)
    assert any("a" in ln and ("1." in ln or "1 " in ln) for ln in lines)
