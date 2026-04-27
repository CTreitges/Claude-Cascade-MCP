"""The cascade-task runner: wires a Telegram update through to run_cascade,
manages the live status message, the heartbeat, and the post-run UI
(rich result, diff chunks, quick-action buttons, skill-suggestion prompt)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from cascade.config import settings
from cascade.core import run_cascade
from cascade.i18n import t
from cascade.memory import remember_fact
from cascade.models import implementer_provider
from cascade.store import Store

from .helpers import (
    fmt_status_emoji,
    format_progress_line,
    lang_for,
    md_escape,
    send_long,
)
from .state import INFLIGHT, PENDING_SKILL
from .typing import TypingIndicator


async def run_task_for_chat(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    task_text: str,
    *,
    attachments: list[Path] | None = None,
    resume_task_id: str | None = None,
) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    s = settings()

    store: Store = ctx.application.bot_data["store"]
    sess = await store.get_chat_session(chat.id) if chat else None
    repo = Path(sess["repo_path"]) if sess and sess.get("repo_path") else None
    impl_model = (sess or {}).get("implementer_model")
    impl_provider = implementer_provider(impl_model) if impl_model else None
    plan_model = (sess or {}).get("planner_model")
    rev_model = (sess or {}).get("reviewer_model")
    plan_effort = (sess or {}).get("planner_effort") or None
    rev_effort = (sess or {}).get("reviewer_effort") or None
    tri_effort = (sess or {}).get("triage_effort") or None
    impl_effort = (sess or {}).get("implementer_effort") or None
    plan_temp = (sess or {}).get("planner_temperature")
    impl_temp = (sess or {}).get("implementer_temperature")
    rev_temp = (sess or {}).get("reviewer_temperature")
    chat_replan_max = (sess or {}).get("replan_max")
    chat_max_iters = (sess or {}).get("max_iterations")
    # Per-chat settings overrides (apply by patching the Settings copy passed
    # to run_cascade — these aren't first-class run_cascade params).
    overrides: dict = {}
    if (raf := (sess or {}).get("replan_after_failures")) is not None:
        overrides["cascade_replan_after_failures"] = int(raf)
    if (te := (sess or {}).get("triage_enabled")) is not None:
        overrides["cascade_triage_enabled"] = bool(te)
    if (ass := (sess or {}).get("auto_skill_suggest")) is not None:
        overrides["cascade_auto_skill_suggest"] = bool(ass)
    if (ce := (sess or {}).get("context7_enabled")) is not None:
        overrides["cascade_context7_enabled"] = bool(ce)
    if (we := (sess or {}).get("websearch_enabled")) is not None:
        overrides["cascade_websearch_enabled"] = bool(we)
    if (mp := (sess or {}).get("multiplan_enabled")) is not None:
        overrides["cascade_multiplan_enabled"] = bool(mp)
    # Apply per-chat model+effort overrides too — so the snapshot-drift check
    # below sees the SAME effective Settings the cascade would actually run with.
    model_overrides: dict = {}
    if plan_model:
        model_overrides["cascade_planner_model"] = plan_model
    if impl_model:
        model_overrides["cascade_implementer_model"] = impl_model
    if rev_model:
        model_overrides["cascade_reviewer_model"] = rev_model
    if plan_effort:
        model_overrides["cascade_planner_effort"] = plan_effort
    if rev_effort:
        model_overrides["cascade_reviewer_effort"] = rev_effort
    if tri_effort:
        model_overrides["cascade_triage_effort"] = tri_effort
    if impl_effort:
        model_overrides["cascade_implementer_effort"] = impl_effort
    if chat_replan_max is not None:
        model_overrides["cascade_replan_max"] = chat_replan_max
    if chat_max_iters is not None:
        model_overrides["cascade_max_iterations"] = chat_max_iters
    if overrides or model_overrides:
        s = s.model_copy(update={**overrides, **model_overrides})

    # Drift-detection: when /resume targets a task whose snapshotted settings
    # differ from the current effective Settings, switch to a fresh restart on
    # the same workspace so the new settings actually take effect (otherwise
    # we'd resume mid-iteration and never hit the new max_iterations cap, etc.).
    drifted_keys: list[str] = []
    if resume_task_id:
        try:
            old_task = await store.get_task(resume_task_id)
        except Exception:
            old_task = None
        if old_task is not None:
            from cascade.core import settings_snapshot_differs
            snapshot = (old_task.metadata or {}).get("start_settings")
            drifted_keys = settings_snapshot_differs(snapshot, s)
            if drifted_keys and old_task.workspace_path:
                # Switch into fresh-with-context mode: drop resume_task_id, pin
                # repo to the existing workspace so iteration counter resets but
                # all written files stay.
                await msg.reply_text(
                    "🆕 Settings haben sich geändert "
                    f"({', '.join(k.replace('cascade_', '') for k in drifted_keys[:3])}"
                    f"{'…' if len(drifted_keys) > 3 else ''}) — starte neu mit selbem "
                    "Workspace, neue Werte greifen ab Iteration 1."
                    if lang_for(update) == "de"
                    else "🆕 Settings changed since this task was created "
                    f"({', '.join(k.replace('cascade_', '') for k in drifted_keys[:3])}"
                    f"{'…' if len(drifted_keys) > 3 else ''}) — restarting fresh on the "
                    "same workspace so the new values apply from iteration 1."
                )
                resume_task_id = None
                repo = Path(old_task.workspace_path)

    lang = lang_for(update)

    # Resume-confirmation: when an interrupted task with a similar text
    # exists in this chat AND the user hasn't already explicitly /resumed,
    # offer an InlineKeyboard "Fortsetzen / Neu starten / Abbrechen". Per
    # user-decided plan this replaces the old free-text confirmation flow.
    if not resume_task_id:
        try:
            recent = await store.list_tasks(limit=10, status="interrupted")
        except Exception:
            recent = []
        from .state import PENDING_RESUME, task_similarity
        candidate = None
        for t_obj in recent:
            sim = task_similarity(task_text, t_obj.task_text or "")
            if sim >= 0.7:
                candidate = (t_obj, sim)
                break
        if candidate is not None:
            cand_task, sim = candidate
            import uuid
            cb_id = uuid.uuid4().hex[:12]
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            PENDING_RESUME[cb_id] = fut
            from .handlers.resume_kbd import make_keyboard
            head = (
                f"♻️ Es gibt einen pausierten Task `{cand_task.id}` "
                f"({int(sim * 100)}% Übereinstimmung):\n_"
                f"{(cand_task.task_text or '')[:200]}_\n\nWas tun?"
            ) if lang == "de" else (
                f"♻️ A paused task `{cand_task.id}` matches this "
                f"({int(sim * 100)}%):\n_"
                f"{(cand_task.task_text or '')[:200]}_\n\nWhat next?"
            )
            from telegram.constants import ParseMode as _PM
            await msg.reply_text(
                head, parse_mode=_PM.MARKDOWN,
                reply_markup=make_keyboard(cb_id, lang=lang),
            )
            try:
                decision = await asyncio.wait_for(fut, timeout=5 * 60)
            except asyncio.TimeoutError:
                decision = "abort"
                PENDING_RESUME.pop(cb_id, None)
            if decision == "abort":
                await msg.reply_text(
                    "🚫 Abgebrochen — kein neuer Run gestartet."
                    if lang == "de"
                    else "🚫 Aborted — no new run started.",
                )
                return
            if decision == "resume":
                resume_task_id = cand_task.id
            # decision == "fresh" → leave resume_task_id None, fall through

    initial = t("progress.planning_initial", lang=lang)
    status_msg = await msg.reply_text(initial)
    cancel = asyncio.Event()

    state = {
        "lines": [initial],
        "skill_suggestion": None,
        "started_at": asyncio.get_event_loop().time(),
        "last_event_at": asyncio.get_event_loop().time(),
        "last_event": "started",
        "current_phase": "planning",
        # Compact-mode bookkeeping: collapse the 4 events of one iter
        # (implementing → implemented → reviewing → reviewed) into ONE
        # updating line. `current_iter_key` holds (subtask_or_None, iter_n);
        # `current_iter_idx` is the index of that line in `lines`.
        "current_iter_key": None,
        "current_iter_idx": None,
        # Per-subtask local iter counter so the user sees "iter 1/2/3"
        # within a sub-task instead of cumulative numbers like "iter 5".
        "subtask_local_iter": {},
        # Counts of {sub-task, iter, replan, etc.} for the final summary.
        "subtasks_done": [],
        "iters_total": 0,
        "replans_total": 0,
        "files_touched": set(),
    }

    def _render() -> str:
        return "\n".join(state["lines"])

    # Milestone events get their own standalone Telegram message inside
    # `progress()` below — much more visible than rolling edits of status_msg.
    # Rolling line still updates for routine events (implementing/reviewing).

    async def progress(task_id: str, event: str, payload: dict) -> None:
        # Update bookkeeping that the heartbeat / final-summary read.
        state["last_event_at"] = asyncio.get_event_loop().time()
        state["last_event"] = event
        # Map the event to a friendly phase label shown by the heartbeat.
        phase_map = {
            "planning": "Planung",
            "planned": "Planung fertig",
            "implementing": "Implementer",
            "implemented": "Implementer fertig",
            "checks_run": "Quality-Checks",
            "reviewing": "Reviewer",
            "reviewed": "Reviewer fertig",
            "replanning": "Re-Planung",
            "replanned": "Re-Planung fertig",
        }
        phase_map_en = {
            "planning": "planning",
            "planned": "plan ready",
            "implementing": "implementer",
            "implemented": "implementer done",
            "checks_run": "quality checks",
            "reviewing": "reviewer",
            "reviewed": "reviewer done",
            "replanning": "replanning",
            "replanned": "new plan ready",
        }
        if event in (phase_map if lang == "de" else phase_map_en):
            state["current_phase"] = (
                phase_map if lang == "de" else phase_map_en
            )[event]
        # Track files for the final summary.
        if event == "implemented":
            for path in payload.get("files_touched") or []:
                state["files_touched"].add(path)
        if event == "reviewed" and payload.get("pass") and payload.get("subtask"):
            state["subtasks_done"].append(payload.get("subtask"))
        if event == "replanned":
            state["replans_total"] += 1
        if event in ("implementing", "implemented"):
            state["iters_total"] = max(
                state["iters_total"], int(payload.get("iteration") or 0),
            )

        if event == "skill_suggested":
            state["skill_suggestion"] = {"task_id": task_id, **payload}
            return

        # Standalone milestone messages — much more visible than rolling edits.
        try:
            if event == "planned":
                steps_n = len(payload.get("steps") or [])
                summary = (payload.get("summary") or "")[:300]
                subs_names = payload.get("subtasks") or []
                subs_n = payload.get("subtasks_count") or len(subs_names)
                from telegram.constants import ParseMode as _PM
                if subs_n and subs_names:
                    sub_list = "\n".join(
                        f"  {i+1}\\. `{n}`" for i, n in enumerate(subs_names)
                    )
                    head = (
                        f"📋 *Plan steht* — {steps_n} Steps, {subs_n} Sub-Tasks\n"
                        f"_{summary}_\n\n"
                        f"🪓 *Sub-Task-Reihenfolge:*\n{sub_list}"
                    ) if lang == "de" else (
                        f"📋 *Plan ready* — {steps_n} steps, {subs_n} sub-tasks\n"
                        f"_{summary}_\n\n"
                        f"🪓 *Sub-task order:*\n{sub_list}"
                    )
                else:
                    head = (
                        f"📋 *Plan steht* ({steps_n} Steps):\n_{summary}_"
                    ) if lang == "de" else (
                        f"📋 *Plan ready* ({steps_n} steps):\n_{summary}_"
                    )
                await msg.reply_text(head, parse_mode=_PM.MARKDOWN)
            elif event == "log" and payload.get("msg", "").startswith("subtask "):
                from telegram.constants import ParseMode as _PM
                await msg.reply_text(
                    f"🪓 {payload['msg']}", parse_mode=_PM.MARKDOWN,
                )
            elif event == "ask_user":
                from telegram.constants import ParseMode as _PM
                question = payload.get("question") or "?"
                # Italicize so it's visually distinct from the running status.
                head = (
                    "❓ *Cascade fragt:*\n_"
                    + question[:600].replace("_", "\\_")
                    + "_\n\n_(antworte mit einer einfachen Telegram-Nachricht — "
                    "das pausiert nichts anderes.)_"
                ) if lang == "de" else (
                    "❓ *Cascade asks:*\n_"
                    + question[:600].replace("_", "\\_")
                    + "_\n\n_(reply with a plain Telegram message — "
                    "this pauses nothing else.)_"
                )
                await msg.reply_text(head, parse_mode=_PM.MARKDOWN)
            elif event == "reviewed" and payload.get("pass") and payload.get("subtask"):
                # Successful sub-task acknowledgment — gives the user a
                # visible "✅" milestone for each slice instead of just the
                # next subtask appearing.
                sub = payload.get("subtask")
                cum_iter = payload.get("iteration")
                from telegram.constants import ParseMode as _PM
                await msg.reply_text(
                    f"✅ Sub-Task `{sub}` abgeschlossen (Iter {cum_iter})"
                    if lang == "de"
                    else f"✅ Sub-Task `{sub}` complete (iter {cum_iter})",
                    parse_mode=_PM.MARKDOWN,
                )
            elif event == "replanning":
                await msg.reply_text(
                    f"🔄 Plan wird neu geschrieben (Replan #{payload.get('replans_done', 0) + 1}) "
                    f"nach Iter {payload.get('after_iteration', '?')}…"
                    if lang == "de"
                    else f"🔄 Replanning (#{payload.get('replans_done', 0) + 1}) "
                    f"after iter {payload.get('after_iteration', '?')}…"
                )
            elif event == "replanned":
                checks = payload.get("checks") or []
                summary = (payload.get("summary") or "")[:200]
                from telegram.constants import ParseMode as _PM
                await msg.reply_text(
                    f"✅ Neuer Plan: _{summary}_\nChecks: {', '.join(checks[:5])}"
                    if lang == "de"
                    else f"✅ New plan: _{summary}_\nChecks: {', '.join(checks[:5])}",
                    parse_mode=_PM.MARKDOWN,
                )
            elif event == "waiting_for_session":
                # Cascade hit a rate-limit / weekly-session-cap and is going
                # to wait. Surface this prominently so the user knows the
                # silence is intentional.
                secs = int(payload.get("seconds") or 0)
                attempt = int(payload.get("attempt") or 1)
                reason = (payload.get("reason") or "").strip()
                if secs >= 86400:
                    when = f"~{secs // 86400}T {(secs % 86400) // 3600}h"
                elif secs >= 3600:
                    when = f"~{secs // 3600}h {(secs % 3600) // 60}min"
                elif secs >= 60:
                    when = f"~{secs // 60}min {secs % 60}s"
                else:
                    when = f"{secs}s"
                from telegram.constants import ParseMode as _PM
                head = (
                    f"⏳ *Warte auf nächste Session* (Versuch {attempt}) — {when}\n"
                    f"_{reason[:200]}_\n"
                    f"_(Cascade läuft automatisch weiter sobald wieder verfügbar.)_\n"
                    f"💡 _Live-Switch zu anderem Provider:_ "
                    f"`/cancel {task_id}` → `/models` → `/resume {task_id}`"
                ) if lang == "de" else (
                    f"⏳ *Waiting for next session window* (attempt {attempt}) — {when}\n"
                    f"_{reason[:200]}_\n"
                    f"_(Cascade resumes automatically when available.)_\n"
                    f"💡 _Live-switch to another provider:_ "
                    f"`/cancel {task_id}` → `/models` → `/resume {task_id}`"
                )
                # Inline keyboard so the user can decide right here: keep
                # waiting OR abort the task. Without this the only way out
                # was typing `/stop <id>` — slow when the rate-limit hits
                # repeatedly. ev is set by the callback; the cascade's
                # _wait_with_cancel sees it and aborts cleanly.
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "✋ Abbrechen" if lang == "de" else "✋ Abort",
                        callback_data=f"wait:{task_id}:abort",
                    ),
                    InlineKeyboardButton(
                        "⏳ Weiter warten" if lang == "de" else "⏳ Keep waiting",
                        callback_data=f"wait:{task_id}:keep",
                    ),
                ]])
                try:
                    await msg.reply_text(
                        head, parse_mode=_PM.MARKDOWN, reply_markup=kb,
                    )
                except Exception:
                    pass
                state["current_phase"] = (
                    f"warte ({when})" if lang == "de" else f"waiting ({when})"
                )
            elif event == "hard_stuck":
                # P1.5: 5+ minutes without ANY progress event.
                # Show the same Abort/Keep-waiting keyboard as the
                # waiting_for_session prompt — taps go through TASK_REGISTRY
                # to the cancel_event so /cancel and the keyboard are
                # interchangeable.
                idle_s = int(payload.get("idle_s") or 0)
                last_event = payload.get("last_event") or "?"
                idle_min = idle_s // 60
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                from telegram.constants import ParseMode as _PM
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "✋ Abbrechen" if lang == "de" else "✋ Abort",
                        callback_data=f"wait:{task_id}:abort",
                    ),
                    InlineKeyboardButton(
                        "⏳ Weiter warten" if lang == "de" else "⏳ Keep waiting",
                        callback_data=f"wait:{task_id}:keep",
                    ),
                ]])
                head = (
                    f"⚠️ *Cascade hängt seit {idle_min} min* "
                    f"(letztes Event: `{last_event}`)\n"
                    f"_Möglicherweise blockiert oder LLM-Call extrem langsam._"
                ) if lang == "de" else (
                    f"⚠️ *Cascade has been silent for {idle_min} min* "
                    f"(last event: `{last_event}`)\n"
                    f"_Maybe blocked or an LLM call is unusually slow._"
                )
                try:
                    await msg.reply_text(
                        head, parse_mode=_PM.MARKDOWN, reply_markup=kb,
                    )
                except Exception:
                    pass
            elif event == "iteration_failed":
                fb = (payload.get("feedback") or "").strip()
                if fb:
                    short = fb[:280] + ("…" if len(fb) > 280 else "")
                    from telegram.constants import ParseMode as _PM
                    await msg.reply_text(
                        f"❌ Iter {payload.get('iteration')} failed:\n_{short}_"
                        if lang == "de"
                        else f"❌ Iter {payload.get('iteration')} failed:\n_{short}_",
                        parse_mode=_PM.MARKDOWN,
                    )
        except Exception:
            pass  # never let UI feedback break the run

        # ---------- Compact rolling status ----------
        # Collapse the 4-event-per-iter chatter (implementing/implemented/
        # reviewing/reviewed) into ONE updating line per iteration. Only
        # other events (planned, replanned, log, failed, …) still get their
        # own line via format_progress_line.

        ITER_EVENTS = ("implementing", "implemented", "reviewing", "reviewed")
        if event in ITER_EVENTS:
            sub = payload.get("subtask")
            cumulative = payload.get("iteration") or 0
            # Local iter within sub-task = how many distinct cumulative-iters
            # we've seen for that subtask so far.
            seen = state["subtask_local_iter"].setdefault(sub, [])
            if cumulative not in seen:
                seen.append(cumulative)
            local = len(seen)
            sub_prefix = f"🪓 `{sub}` " if sub else ""
            cum_suffix = f" _(total {cumulative})_" if sub else ""
            # Build the right "phase" marker for this event.
            if event == "implementing":
                line = f"{sub_prefix}*iter {local}* ⏳ implementiert …{cum_suffix}"
            elif event == "implemented":
                ops = payload.get("ops", 0)
                fail = payload.get("failed", 0)
                line = (
                    f"{sub_prefix}*iter {local}* ⚙️ {ops} Ops"
                    + (f" · {fail} ❌" if fail else "")
                    + f"{cum_suffix}"
                )
            elif event == "reviewing":
                line = f"{sub_prefix}*iter {local}* 🔍 prüfe …{cum_suffix}"
            else:  # reviewed
                if payload.get("pass"):
                    line = f"{sub_prefix}*iter {local}* ✅ pass{cum_suffix}"
                else:
                    fb = (payload.get("feedback") or "").strip().splitlines()
                    fb_first = fb[0][:90] if fb else ""
                    suffix = f": _{fb_first}_" if fb_first else ""
                    line = f"{sub_prefix}*iter {local}* ❌ fail{suffix}{cum_suffix}"

            iter_key = (sub, cumulative)
            if state["current_iter_key"] == iter_key and state["current_iter_idx"] is not None:
                idx = state["current_iter_idx"]
                if 0 <= idx < len(state["lines"]):
                    state["lines"][idx] = line
                else:
                    state["lines"].append(line)
                    state["current_iter_idx"] = len(state["lines"]) - 1
            else:
                state["lines"].append(line)
                state["current_iter_key"] = iter_key
                state["current_iter_idx"] = len(state["lines"]) - 1
        else:
            # Non-iter events (log, planned, replanned, failed, …) get
            # their own dedicated line as before.
            line = format_progress_line(event, payload, lang)
            if not line:
                return
            state["lines"].append(line)
            # New non-iter line invalidates the "current iter" tracker so
            # the next implementing/etc. starts a fresh line.
            state["current_iter_key"] = None
            state["current_iter_idx"] = None

        if len(state["lines"]) > 12:
            state["lines"] = [state["lines"][0], "  …"] + state["lines"][-10:]
            # indices shifted; safest is to reset the iter pointer
            state["current_iter_key"] = None
            state["current_iter_idx"] = None
        try:
            from telegram.constants import ParseMode as _PM
            # status_msg may have been replaced by the heartbeat — read
            # whatever the *current* message is from state.
            current = state.get("status_msg") or status_msg
            await current.edit_text(_render(), parse_mode=_PM.MARKDOWN)
        except Exception:
            try:
                current = state.get("status_msg") or status_msg
                await current.edit_text(_render())
            except Exception:
                pass

    coro = run_cascade(
        task=task_text,
        source="telegram",
        repo=repo,
        attachments=attachments,
        implementer_model=impl_model,
        implementer_provider=impl_provider,
        planner_model=plan_model,
        reviewer_model=rev_model,
        planner_effort=plan_effort,
        reviewer_effort=rev_effort,
        triage_effort=tri_effort,
        implementer_effort=impl_effort,
        planner_temperature=plan_temp,
        implementer_temperature=impl_temp,
        reviewer_temperature=rev_temp,
        replan_max=chat_replan_max,
        max_iterations=chat_max_iters,
        lang=lang,
        progress=progress,
        s=s,
        store=store,
        cancel_event=cancel,
        resume_task_id=resume_task_id,
    )
    task_obj = asyncio.create_task(coro)

    # Mutable holder so register_when_known can publish the discovered
    # task_id back to the outer scope (for cleanup in the finally block).
    self_task_id: dict[str, str | None] = {"id": None}

    async def register_when_known() -> None:
        for _ in range(30):
            await asyncio.sleep(0.1)
            latest = await store.latest_task()
            if latest and latest.task_text == task_text:
                INFLIGHT[chat.id] = (latest.id, task_obj, cancel)
                # Also register by task-id so /stop <id> finds tasks even
                # when a *newer* task in the same chat overwrites the
                # INFLIGHT[chat] slot.
                from .state import TASK_REGISTRY
                TASK_REGISTRY[latest.id] = cancel
                self_task_id["id"] = latest.id
                if chat:
                    await store.set_chat_last_task(chat.id, latest.id)
                return

    asyncio.create_task(register_when_known())

    HB_MARKER = "​"
    # 60-second heartbeat. Behaviour change (user request 2026-04-27):
    # instead of edit-in-place on the original status_msg (which scrolls
    # off-screen as the user keeps chatting), the heartbeat REPOSTS the
    # full status as a NEW Telegram message every interval AND deletes
    # the previous one. Net effect: a single live-progress card that
    # follows the bottom of the chat.
    HEARTBEAT_INTERVAL_S = 60
    HEARTBEAT_IDLE_THRESHOLD_S = 30
    # `state["status_msg"]` is the *current* message holding the live
    # progress card. The progress() callback above edits it; the
    # heartbeat reposts it.
    state["status_msg"] = status_msg

    async def heartbeat() -> None:
        spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        i = 0
        while not task_obj.done():
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            if task_obj.done():
                return
            now = asyncio.get_event_loop().time()
            idle = now - state.get("last_event_at", state["started_at"])
            elapsed = int(now - state["started_at"])
            mark = spinner[i % len(spinner)]
            i += 1
            phase = state.get("current_phase") or state.get("last_event") or "?"
            if idle < HEARTBEAT_IDLE_THRESHOLD_S:
                # Events are flowing — no need to repost yet, the original
                # status_msg edits are still visible-ish.
                continue
            # Format elapsed as `Xm Ys` for runs >60s (more legible than
            # `347s`). Sub-minute runs keep the seconds-only form.
            if elapsed >= 60:
                m, sec = divmod(elapsed, 60)
                elapsed_str = f"{m}m {sec:02d}s"
            else:
                elapsed_str = f"{elapsed}s"
            # Also surface idle-time once it gets noticeable — helps the
            # user judge whether things are still moving (idle small) or
            # stuck on one slow LLM call (idle approaching heartbeat).
            idle_int = int(idle)
            idle_suffix = (
                f" · idle {idle_int}s" if idle_int >= 30 else ""
            )
            if lang == "de":
                tag = (
                    f"{HB_MARKER}  {mark} noch dran — *{phase}* "
                    f"({elapsed_str}{idle_suffix})"
                )
            else:
                tag = (
                    f"{HB_MARKER}  {mark} still working — *{phase}* "
                    f"({elapsed_str}{idle_suffix})"
                )
            lines = state["lines"]
            if lines and lines[-1].startswith(HB_MARKER):
                lines[-1] = tag
            else:
                lines.append(tag)

            # Repost: send a fresh message with the current rendered
            # status, then delete the old one. The new message becomes
            # the new edit-target so subsequent progress() events
            # update IT in place until the next 60s tick.
            from telegram.constants import ParseMode as _PM
            old = state["status_msg"]
            try:
                new = await msg.reply_text(_render(), parse_mode=_PM.MARKDOWN)
            except Exception:
                # Markdown parse failed → plain text fallback
                try:
                    new = await msg.reply_text(_render())
                except Exception:
                    new = None
            if new is None:
                continue
            state["status_msg"] = new
            try:
                await old.delete()
            except Exception:
                pass  # old message may already be gone — non-fatal

    hb_task = asyncio.create_task(heartbeat())

    try:
        async with TypingIndicator(ctx, chat.id):
            try:
                result = await task_obj
            except asyncio.CancelledError:
                await msg.reply_text(t("result.cancelled", lang=lang))
                return
            except Exception as e:
                from cascade.error_log import log_error
                await log_error(
                    "runner.run_cascade", e,
                    chat_id=chat.id,
                    task_text=task_text[:200],
                    impl_model=impl_model,
                    plan_model=plan_model,
                    rev_model=rev_model,
                )
                await msg.reply_text(t("result.crashed", lang=lang, error=str(e)))
                return
    finally:
        hb_task.cancel()
        try:
            await hb_task
        except (asyncio.CancelledError, Exception):
            pass
        # Only pop INFLIGHT if it still points at *us* — a newer task in the
        # same chat may have overwritten the slot, and we mustn't clobber it.
        cur = INFLIGHT.get(chat.id)
        if cur and cur[1] is task_obj:
            INFLIGHT.pop(chat.id, None)
        from .state import TASK_REGISTRY
        own_id = self_task_id.get("id")
        if own_id:
            TASK_REGISTRY.pop(own_id, None)

    # Persist task outcome into RLM and the chat overview so the bot can
    # answer "what did you just build?" without re-running the cascade.
    try:
        files_str = (
            ", ".join(result.changed_files[:8]) if result.changed_files else "—"
        )
        await remember_fact(
            f"[chat {chat.id}] task {result.task_id} {result.status} after "
            f"{result.iterations} iter(s); workspace={result.workspace_path}; "
            f"changed=[{files_str}]; summary={result.summary[:300] if result.summary else '—'}",
            importance="high" if result.status == "done" else "medium",
            tags=f"cascade-bot-mcp,telegram-chat,chat-{chat.id},task-result,"
                 f"task-{result.task_id}",
        )
        await store.append_chat_message(
            chat.id, "bot",
            f"[task {result.status}] {result.task_id}: "
            f"{(result.summary or '')[:160]}",
        )
    except Exception as e:  # never fail the user-facing flow because of memory
        from logging import getLogger
        getLogger("cascade.bot.runner").debug("post-run memory failed: %s", e)

    # Compact one-line summary card on top of the rich report. Pulls counts
    # from the runner's local state (filled inside progress()) — independent
    # of `result` so it survives even if run_cascade returned partial info.
    elapsed_total = int(asyncio.get_event_loop().time() - state["started_at"])
    minutes, seconds = divmod(elapsed_total, 60)
    if minutes:
        dur_str = (f"{minutes}m {seconds}s" if lang == "en" else f"{minutes}min {seconds}s")
    else:
        dur_str = f"{seconds}s"
    n_subtasks = len(state["subtasks_done"])
    n_iters = state["iters_total"] or result.iterations
    n_replans = state["replans_total"]
    n_files = len(state["files_touched"]) or len(result.changed_files or [])
    status_emoji = fmt_status_emoji(result.status)
    if lang == "de":
        bits = [f"{n_iters} iter"]
        if n_subtasks:
            bits.insert(0, f"{n_subtasks} Sub-Tasks")
        if n_replans:
            bits.append(f"{n_replans} Replans")
        if n_files:
            bits.append(f"{n_files} Dateien")
        bits.insert(0, dur_str)
        compact_card = (
            f"{status_emoji} *Fertig* — `{result.task_id}` ({', '.join(bits)})"
        )
    else:
        bits = [f"{n_iters} iters"]
        if n_subtasks:
            bits.insert(0, f"{n_subtasks} sub-tasks")
        if n_replans:
            bits.append(f"{n_replans} replans")
        if n_files:
            bits.append(f"{n_files} files")
        bits.insert(0, dur_str)
        compact_card = (
            f"{status_emoji} *Done* — `{result.task_id}` ({', '.join(bits)})"
        )

    # Rich final report. summary/plan are AI-generated free text, so escape
    # any markdown specials before splicing them into the markdown template
    # — otherwise unbalanced backticks/underscores crash sendMessage.
    header = t(
        "result.summary",
        lang=lang,
        emoji=fmt_status_emoji(result.status),
        status=result.status,
        task_id=result.task_id,
        iterations=result.iterations,
        workspace=result.workspace_path,
        summary=md_escape(result.summary or ""),
    )
    parts = [compact_card, "", header]
    if result.plan and result.plan.summary:
        label = "*Plan:*"
        parts.append(f"\n{label} {md_escape(result.plan.summary[:400])}")

    # Sub-task summary — for decomposed runs, one line per sub-task with
    # its outcome derived from the iteration log we captured along the way.
    if result.plan and getattr(result.plan, "subtasks", None):
        try:
            iters = await store.list_iterations(result.task_id)
            # Map subtask name → list[Iteration]; subtask name is stored as
            # JSON in implementer_output.
            import json as _json
            per_sub: dict[str, list] = {}
            for it in iters:
                if it.n == 0 or not it.implementer_output:
                    continue
                try:
                    payload = _json.loads(it.implementer_output)
                except Exception:
                    continue
                sub = payload.get("subtask")
                if sub:
                    per_sub.setdefault(sub, []).append(it)
            label = "*Sub-Task-Übersicht:*" if lang == "de" else "*Sub-task summary:*"
            sub_lines = []
            for sub in result.plan.subtasks:
                its = per_sub.get(sub.name, [])
                if not its:
                    sub_lines.append(f"  ⏸ `{sub.name}` _(nicht erreicht)_")
                    continue
                last = its[-1]
                if last.reviewer_pass:
                    sub_lines.append(
                        f"  ✅ `{sub.name}` — {len(its)} iter"
                    )
                else:
                    fb_first = (last.reviewer_feedback or "").split("\n")[0][:80]
                    sub_lines.append(
                        f"  ❌ `{sub.name}` — {len(its)} iter"
                        + (f": _{md_escape(fb_first)}_" if fb_first else "")
                    )
            if sub_lines:
                parts.append(f"\n{label}\n" + "\n".join(sub_lines))
        except Exception:
            pass

    if result.changed_files:
        label = "*Geänderte Dateien:*" if lang == "de" else "*Changed files:*"
        files_block = "\n".join(f"  • `{f}`" for f in result.changed_files[:15])
        more = (
            f"\n  … +{len(result.changed_files) - 15} weitere"
            if len(result.changed_files) > 15
            else ""
        )
        parts.append(f"\n{label}\n{files_block}{more}")
    full_msg = "\n".join(parts)
    if len(full_msg) > 3800:
        full_msg = full_msg[:3800] + "…"

    if result.status in ("done", "failed") and result.task_id:
        action_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🔄 " + ("Nochmal" if lang == "de" else "Again"),
                callback_data=f"act:again:{result.task_id}",
            ),
            InlineKeyboardButton(
                "📄 Diff",
                callback_data=f"act:diff:{result.task_id}",
            ),
            InlineKeyboardButton(
                "🔁 Resume",
                callback_data=f"act:resume:{result.task_id}",
            ),
        ]])
        await msg.reply_text(full_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=action_kb)
    else:
        await msg.reply_text(full_msg, parse_mode=ParseMode.MARKDOWN)

    if result.diff and result.diff.strip():
        await send_long(msg, result.diff, code=True, chunk=3500)

    if result.status == "done" and state.get("skill_suggestion"):
        sug = state["skill_suggestion"]
        from cascade.skill_suggester import SkillSuggestion, format_skill_proposal
        try:
            sug_obj = SkillSuggestion.model_validate(
                {k: v for k, v in sug.items() if k != "task_id"} | {"should_create": True}
            )
            text = format_skill_proposal(sug_obj, lang=lang)
        except Exception:
            text = f"💡 Skill-Vorschlag: `{sug.get('name')}`"
        PENDING_SKILL[chat.id] = sug
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "💾 " + ("Speichern" if lang == "de" else "Save"),
                callback_data=f"sk:y:{sug['name']}",
            ),
            InlineKeyboardButton(
                "❌ " + ("Verwerfen" if lang == "de" else "Discard"),
                callback_data=f"sk:n:{sug['name']}",
            ),
        ]])
        await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
