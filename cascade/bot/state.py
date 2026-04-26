"""Process-wide bot state. Kept tiny on purpose so handlers can be split
across modules without circular imports."""

from __future__ import annotations

import asyncio

# chat_id → (task_id, asyncio.Task, cancel_event)
INFLIGHT: dict[int, tuple[str, asyncio.Task, asyncio.Event]] = {}

# chat_id → "de" | "en"; falls back to settings.cascade_bot_lang
LANG_OVERRIDE: dict[int, str] = {}

# chat_id → {task_id, name, description, task_template, ...}
PENDING_SKILL: dict[int, dict] = {}

# Resume-confirmation keyboard awaiting decision: callback_id → asyncio.Future.
# When the user taps "Fortsetzen" / "Neu starten" / "Abbrechen", the callback
# handler sets the Future, and run_task_for_chat continues on the chosen path.
PENDING_RESUME: dict[str, asyncio.Future] = {}

def _tokens(text: str) -> set[str]:
    if not text:
        return set()
    out = set()
    cur = []
    for ch in text.lower():
        if ch.isalnum():
            cur.append(ch)
        else:
            if cur:
                tok = "".join(cur)
                cur = []
                if len(tok) >= 4:
                    out.add(tok)
    if cur:
        tok = "".join(cur)
        if len(tok) >= 4:
            out.add(tok)
    return out


def task_similarity(a: str, b: str) -> float:
    """Lightweight Jaccard on word tokens. Used to decide whether a freshly-
    submitted task text matches an interrupted one well enough to offer the
    user a Resume keyboard. 0.0 means no overlap, 1.0 means identical."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]
# 999 acts as "unlimited" — the cascade loops won't realistically hit it.
UNLIMITED_SENTINEL = 999
REPLAN_CHOICES = [0, 1, 2, 3, 5, 10, UNLIMITED_SENTINEL]
ITERATION_CHOICES = [3, 5, 8, 12, 20, UNLIMITED_SENTINEL]
GIT_WHITELIST = {"status", "log", "diff", "branch", "checkout", "pull", "push", "commit", "show"}
