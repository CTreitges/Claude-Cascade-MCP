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

EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]
REPLAN_CHOICES = [0, 1, 2, 3, 5]
GIT_WHITELIST = {"status", "log", "diff", "branch", "checkout", "pull", "push", "commit", "show"}
