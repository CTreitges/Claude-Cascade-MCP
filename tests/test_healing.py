from __future__ import annotations

import asyncio

import pytest

from cascade.healing import (
    HealingConfig,
    HealingMonitor,
    HealingState,
    diagnose_empty_subtasks,
    diagnose_permission_issue,
)


def test_diagnose_permission_detects_common_markers():
    assert diagnose_permission_issue("Permission denied: /etc/x")
    assert diagnose_permission_issue("[Errno 13] EACCES")
    assert diagnose_permission_issue("operation not permitted")
    assert diagnose_permission_issue("totally fine") is None
    assert diagnose_permission_issue("") is None


def test_diagnose_empty_subtasks_only_warns_when_decompose_on():
    plan_empty = {"subtasks": []}
    plan_full = {"subtasks": [{"name": "x"}]}
    assert diagnose_empty_subtasks(plan_empty, auto_decompose=True) is not None
    assert diagnose_empty_subtasks(plan_empty, auto_decompose=False) is None
    assert diagnose_empty_subtasks(plan_full, auto_decompose=True) is None


def test_state_mark_event_resets_idle_counter():
    st = HealingState()
    first = st.last_event_at
    st.mark_event("iter 1 started")
    assert st.last_event == "iter 1 started"
    # Either equal-or-greater monotonic time
    assert st.last_event_at >= first


def test_state_mark_log_text_keeps_rolling_window():
    st = HealingState()
    for i in range(30):
        st.mark_log_text(f"line {i}")
    assert len(st.recent_logs) == 20
    assert st.recent_logs[0] == "line 10"
    assert st.recent_logs[-1] == "line 29"


async def test_monitor_emits_stuck_alert_after_threshold():
    """The monitor must surface a stuck-alert event when idle exceeds the
    alert threshold — the runner shows this as a heartbeat."""
    events: list[tuple[str, dict]] = []

    async def fake_progress(task_id, event, payload):
        events.append((event, payload))

    state = HealingState()
    # Make state look "old" relative to monotonic clock
    state.last_event_at -= 500.0  # 500s ago
    cfg = HealingConfig(
        check_interval_s=0.05,
        stuck_threshold_s=10.0,
        stuck_alert_threshold_s=20.0,
        # Push hard_stuck above the simulated idle (500s) so this test
        # exercises the alert tier, not the hard_stuck tier.
        hard_stuck_threshold_s=10_000.0,
    )
    async with HealingMonitor(state, fake_progress, "abc123", config=cfg):
        await asyncio.sleep(0.2)  # let the loop tick a few times

    alerts = [(e, p) for e, p in events if p.get("kind") == "stuck-alert"]
    assert alerts, f"expected at least one stuck-alert; got: {events}"
    assert "abc123" not in str(alerts[0][1].get("msg", ""))  # task_id is a separate arg


async def test_monitor_emits_permission_diagnosis_once():
    events: list[tuple[str, dict]] = []

    async def fake_progress(task_id, event, payload):
        events.append((event, payload))

    state = HealingState()
    state.recent_review_feedback = "Could not write file: Permission denied"
    cfg = HealingConfig(
        check_interval_s=0.05,
        stuck_threshold_s=10.0,
        stuck_alert_threshold_s=10_000.0,  # disable stuck branch
    )
    async with HealingMonitor(state, fake_progress, "abc", config=cfg):
        await asyncio.sleep(0.25)  # let multiple ticks happen

    perm_events = [(e, p) for e, p in events if p.get("kind") == "permission-issue"]
    assert len(perm_events) == 1, "should emit perm-issue exactly once, not on every tick"


async def test_monitor_disabled_does_nothing():
    events: list[tuple[str, dict]] = []

    async def fake_progress(task_id, event, payload):
        events.append((event, payload))

    state = HealingState()
    state.last_event_at -= 1_000_000  # very old
    cfg = HealingConfig(enabled=False)
    async with HealingMonitor(state, fake_progress, "x", config=cfg):
        await asyncio.sleep(0.1)
    assert events == []


async def test_monitor_recovers_when_event_arrives():
    events: list[tuple[str, dict]] = []

    async def fake_progress(task_id, event, payload):
        events.append((event, payload))

    state = HealingState()
    state.last_event_at -= 500.0  # stuck
    cfg = HealingConfig(
        check_interval_s=0.03,
        stuck_threshold_s=10.0,
        stuck_alert_threshold_s=20.0,
    )
    async with HealingMonitor(state, fake_progress, "x", config=cfg):
        await asyncio.sleep(0.1)
        # Simulate a fresh event arriving — the monitor's next tick should
        # see idle reset and stop emitting alerts.
        before = len([e for e, p in events if p.get("kind") == "stuck-alert"])
        state.mark_event("iter 2 implementing")
        await asyncio.sleep(0.4)
        # Within the alert refresh window (= alert threshold), the monitor
        # waits before re-alerting. After mark_event the alerts shouldn't
        # double in number.
        after = len([e for e, p in events if p.get("kind") == "stuck-alert"])
        assert after == before, "monitor kept alerting after event arrived"


async def test_state_mark_implementer_output_tracks_hashes():
    st = HealingState()
    st.mark_implementer_output('{"ops":[{"op":"write","path":"a"}]}')
    st.mark_implementer_output('{"ops":[{"op":"write","path":"a"}]}')
    st.mark_implementer_output('{"ops":[{"op":"write","path":"b"}]}')
    assert len(st.recent_impl_hashes) == 3
    assert st.recent_impl_hashes[0] == st.recent_impl_hashes[1]
    assert st.recent_impl_hashes[1] != st.recent_impl_hashes[2]
    # Ring-buffer bound is 6
    for i in range(10):
        st.mark_implementer_output(f'{{"i":{i}}}')
    assert len(st.recent_impl_hashes) == 6


async def test_monitor_emits_implementer_stuck_after_three_identical():
    events: list[tuple[str, dict]] = []

    async def fake_progress(task_id, event, payload):
        events.append((event, payload))

    state = HealingState()
    same = '{"ops":[{"op":"write","path":"x"}]}'
    state.mark_implementer_output(same)
    state.mark_implementer_output(same)
    state.mark_implementer_output(same)

    cfg = HealingConfig(
        check_interval_s=0.03,
        stuck_threshold_s=10_000.0,
        stuck_alert_threshold_s=10_000.0,
    )
    async with HealingMonitor(state, fake_progress, "x", config=cfg):
        await asyncio.sleep(0.15)

    stuck = [(e, p) for e, p in events if p.get("kind") == "implementer-stuck"]
    assert len(stuck) == 1, f"expected exactly one impl-stuck event, got: {events}"


async def test_monitor_implementer_stuck_does_not_double_emit():
    events: list[tuple[str, dict]] = []

    async def fake_progress(task_id, event, payload):
        events.append((event, payload))

    state = HealingState()
    same = "abc"
    for _ in range(4):
        state.mark_implementer_output(same)

    cfg = HealingConfig(
        check_interval_s=0.03,
        stuck_threshold_s=10_000.0,
        stuck_alert_threshold_s=10_000.0,
    )
    async with HealingMonitor(state, fake_progress, "x", config=cfg):
        await asyncio.sleep(0.2)
    stuck = [(e, p) for e, p in events if p.get("kind") == "implementer-stuck"]
    assert len(stuck) == 1


@pytest.mark.parametrize("kind,text", [
    ("logs", "permission denied: /etc/foo"),
    ("review", "EACCES while writing /home/x"),
])
async def test_monitor_picks_up_either_source(kind, text):
    events: list[tuple[str, dict]] = []

    async def fake_progress(task_id, event, payload):
        events.append((event, payload))

    state = HealingState()
    if kind == "logs":
        state.mark_log_text(text)
    else:
        state.recent_review_feedback = text
    cfg = HealingConfig(
        check_interval_s=0.03,
        stuck_threshold_s=10.0,
        stuck_alert_threshold_s=10_000.0,
    )
    async with HealingMonitor(state, fake_progress, "x", config=cfg):
        await asyncio.sleep(0.1)
    perm = [(e, p) for e, p in events if p.get("kind") == "permission-issue"]
    assert perm
