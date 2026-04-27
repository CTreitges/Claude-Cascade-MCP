"""HealingMonitor — observes a running cascade and surfaces diagnostics.

The cascade already has solid resilience layers:
  - `with_retry` handles rate-limits / timeouts / transient subprocess kills
  - `Workspace.acquire_lock` reclaims stale locks
  - The supervisor auto-replans after configurable failures
  - The bot lifecycle marks orphan running tasks as `interrupted`

This module is the LAST line of defence: the *observer* that watches a
single run and emits diagnostic events when something looks off, without
aggressively killing anything (that's `with_retry`'s job). Specifically:

  - **stuck detection** — when more than `stuck_threshold_s` (default 90s)
    elapse without any progress event, emit a "log" event with a diagnosis
    that the bot's runner can show as a heartbeat. After a longer window
    (`stuck_alert_threshold_s`, default 180s) escalate to a one-shot
    `log` event tagged "stuck-alert" so the user knows something is
    really hanging — but never kill the process.

  - **permission-denied surfacing** — when an iteration's reviewer feedback
    or implementer log mentions "permission denied", flag it as a likely
    user-action-needed (e.g. wrong file ownership, missing sudo).

  - **empty-subtask detection** — if the planner returned an empty
    `subtasks` list and `auto_decompose` was on, that's a dead-end plan;
    flag it so the supervisor can replan rather than burn iterations.

The monitor is started by `run_cascade` as a background `asyncio.Task` and
cancelled at the end of the run. It never touches the workspace itself —
all interventions go through `progress` events so the runner can decide
how to surface them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger("cascade.healing")


ProgressLike = Callable[[str, str, dict], Awaitable[None]]


@dataclass
class HealingState:
    """Mutable state the monitor reads. Update from the runner via
    `mark_event`/`mark_log_text`/`mark_iteration`. The monitor never
    writes to it — that keeps the runner as single source of truth."""

    started_at: float = field(default_factory=time.monotonic)
    last_event_at: float = field(default_factory=time.monotonic)
    last_event: str = "started"
    iteration: int = 0
    subtask: str | None = None
    # Texts the monitor scans for diagnostic patterns.
    recent_logs: list[str] = field(default_factory=list)
    recent_review_feedback: str = ""
    # Hashes of the last few `implementer_output` strings — used to spot
    # the case where the implementer keeps producing the same diff (a
    # complement to reviewer-feedback stagnation in core.py).
    recent_impl_hashes: list[str] = field(default_factory=list)
    # Set to True by the monitor when 3 consecutive implementer outputs
    # have identical hashes. Read by the cascade loop to escalate into a
    # forced replan instead of just logging — a stuck implementer that
    # ignores reviewer feedback won't unstick on its own.
    implementer_stuck: bool = False

    def mark_event(self, event: str) -> None:
        self.last_event = event
        self.last_event_at = time.monotonic()

    def mark_iteration(self, n: int, subtask: str | None = None) -> None:
        self.iteration = n
        self.subtask = subtask
        self.mark_event(f"iter {n}" + (f"/{subtask}" if subtask else ""))

    def mark_log_text(self, text: str) -> None:
        if not text:
            return
        self.recent_logs.append(text)
        # keep last 20
        if len(self.recent_logs) > 20:
            self.recent_logs = self.recent_logs[-20:]

    def mark_implementer_output(self, output: str) -> None:
        """Hash and remember the implementer's serialized output. The
        monitor flags 3-in-a-row identical hashes as `implementer-stuck`."""
        if not output:
            return
        import hashlib
        h = hashlib.sha1(output.encode("utf-8", errors="replace")).hexdigest()[:16]
        self.recent_impl_hashes.append(h)
        if len(self.recent_impl_hashes) > 6:
            self.recent_impl_hashes = self.recent_impl_hashes[-6:]


@dataclass
class HealingConfig:
    check_interval_s: float = 15.0
    stuck_threshold_s: float = 90.0
    stuck_alert_threshold_s: float = 180.0
    # P1.5: when there's been NO progress event for this long, the
    # monitor escalates by emitting a `hard_stuck` event that the
    # runner surfaces as an inline keyboard ("Abort / Keep waiting").
    # Without this, a hung subprocess just keeps logging "still within
    # tolerance" forever.
    hard_stuck_threshold_s: float = 300.0
    enabled: bool = True


_PERMISSION_RX = (
    "permission denied",
    "operation not permitted",
    "eacces",
    "errno 13",
)


def diagnose_permission_issue(text: str) -> str | None:
    """Return a short user-facing diagnosis if `text` looks like a
    permission problem — None otherwise."""
    if not text:
        return None
    low = text.lower()
    for marker in _PERMISSION_RX:
        if marker in low:
            return (
                "permission-denied detected — the cascade can't write or read "
                "a path it expected to. Check file ownership / chmod, or "
                "set CASCADE_HOME to a writable location."
            )
    return None


def diagnose_empty_subtasks(plan_dict: dict, *, auto_decompose: bool) -> str | None:
    """If the planner returned an empty subtasks list while decomposition
    was on, that's a dead-end plan. Return a short diagnosis."""
    if not auto_decompose:
        return None
    subs = plan_dict.get("subtasks") or []
    if subs:
        return None
    return (
        "planner returned 0 sub-tasks despite auto_decompose=True — this "
        "usually means the task was small enough for direct iteration or "
        "the planner couldn't see the decomposition."
    )


class HealingMonitor:
    """Background watcher. Use as:

        state = HealingState()
        async with HealingMonitor(state, progress, task_id):
            # run the cascade — update `state` along the way
            ...
    """

    def __init__(
        self,
        state: HealingState,
        progress: ProgressLike,
        task_id: str,
        *,
        config: HealingConfig | None = None,
    ) -> None:
        self.state = state
        self.progress = progress
        self.task_id = task_id
        self.config = config or HealingConfig()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_alert_emitted_at: float = 0.0
        self._last_perm_emitted_for: str = ""
        self._last_hard_stuck_emitted_at: float = 0.0
        self._last_impl_stuck_hash: str = ""

    async def __aenter__(self) -> "HealingMonitor":
        if self.config.enabled:
            self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.config.check_interval_s,
                )
                return  # stop signalled
            except asyncio.TimeoutError:
                pass
            try:
                await self._tick()
            except Exception as e:  # never let the watcher die
                log.warning("healing tick failed: %s", e)

    async def _tick(self) -> None:
        now = time.monotonic()
        idle = now - self.state.last_event_at

        # 1) Stuck detection — three escalation tiers:
        #    - hard_stuck_threshold_s (default 300s): emit `hard_stuck`
        #      progress event so the runner can surface an inline keyboard
        #      and let the user decide. (P1.5)
        #    - stuck_alert_threshold_s (default 180s): WARNING + log-event,
        #      visible in the chat as a "still working" notice.
        #    - stuck_threshold_s (default 90s): DEBUG-only chatter.
        if idle > self.config.hard_stuck_threshold_s:
            # Emit at most once per hard_stuck_threshold_s window so the
            # user doesn't get spammed. The runner shows the keyboard;
            # tapping Abort sets the cancel_event the same way /cancel
            # would. Tapping "keep waiting" just dismisses the keyboard.
            if (now - self._last_hard_stuck_emitted_at) > self.config.hard_stuck_threshold_s:
                msg = (
                    f"hard-stuck: {idle:.0f}s without ANY progress event "
                    f"(last={self.state.last_event!r}, iter={self.state.iteration})"
                )
                log.warning(msg)
                await _safe_emit(
                    self.progress, self.task_id, "hard_stuck",
                    {
                        "msg": msg,
                        "idle_s": int(idle),
                        "last_event": self.state.last_event,
                        "iteration": self.state.iteration,
                    },
                )
                self._last_hard_stuck_emitted_at = now
                self._last_alert_emitted_at = now  # don't double-emit
        elif idle > self.config.stuck_alert_threshold_s:
            # only emit one alert per stuck-window, refreshed every threshold
            if (now - self._last_alert_emitted_at) > self.config.stuck_alert_threshold_s:
                msg = (
                    f"healing: {idle:.0f}s idle since last event "
                    f"(last={self.state.last_event!r}, iter={self.state.iteration})"
                )
                log.warning(msg)
                await _safe_emit(
                    self.progress, self.task_id, "log",
                    {"msg": msg, "kind": "stuck-alert"},
                )
                self._last_alert_emitted_at = now
        elif idle > self.config.stuck_threshold_s:
            # DEBUG-level: this fires every tick during a normal long LLM call
            # (planner ~2min, implementer ~3min). At INFO-level it produces
            # 12+ identical lines per minute — pure log noise. The actual
            # alert at stuck_alert_threshold_s above is still WARNING and
            # also surfaced to the user via the progress stream.
            log.debug(
                "healing: %.0fs idle (last=%s, iter=%d) — still within tolerance",
                idle, self.state.last_event, self.state.iteration,
            )

        # 2) Permission-denied scan (recent reviewer feedback + logs)
        haystack = "\n".join(self.state.recent_logs[-5:])
        if self.state.recent_review_feedback:
            haystack += "\n" + self.state.recent_review_feedback
        diag = diagnose_permission_issue(haystack)
        if diag and diag != self._last_perm_emitted_for:
            log.warning("healing: %s", diag)
            await _safe_emit(
                self.progress, self.task_id, "log",
                {"msg": diag, "kind": "permission-issue"},
            )
            self._last_perm_emitted_for = diag

        # 3) Implementer-stuck: 3 identical outputs in a row → flag once.
        # The reviewer-feedback stagnation in core.py already triggers
        # replan when the REVIEWER says the same thing twice. This catch
        # is for the rarer case where the implementer regenerates an
        # identical diff regardless of feedback (e.g. it ignores it).
        hashes = self.state.recent_impl_hashes
        if (
            len(hashes) >= 3
            and hashes[-1] == hashes[-2] == hashes[-3]
            and self._last_impl_stuck_hash != hashes[-1]
        ):
            msg = (
                f"implementer-stuck: 3 identical outputs (hash={hashes[-1]}) — "
                "implementer is ignoring reviewer feedback. Force replan or "
                "switch implementer model."
            )
            log.warning("healing: %s", msg)
            await _safe_emit(
                self.progress, self.task_id, "log",
                {"msg": msg, "kind": "implementer-stuck"},
            )
            self._last_impl_stuck_hash = hashes[-1]
            # Flag the shared state so the cascade loop's next iter
            # checkpoint reads it and forces a replan. Without this the
            # implementer would just keep echoing the same diff forever
            # (reviewer keeps rejecting, implementer keeps ignoring).
            self.state.implementer_stuck = True


async def _safe_emit(
    progress: ProgressLike, task_id: str, event: str, payload: dict,
) -> None:
    try:
        await progress(task_id, event, payload)
    except Exception as e:
        log.debug("healing emit failed: %s", e)
