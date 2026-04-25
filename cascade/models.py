"""Curated catalog of supported models for each worker.

Tags verified against Ollama Cloud's /v1/models response (2026-04-25).
Add or rename entries here; the bot's /models command picks them up automatically.
"""

from __future__ import annotations

# tag → (display name, provider)
# Curated user-facing list shown in the Telegram /models menu.
# qwen3-coder:480b stays as the runtime default (CASCADE_IMPLEMENTER_MODEL),
# but the menu only offers the four explicitly requested cloud options.
IMPLEMENTER_MODELS: dict[str, tuple[str, str]] = {
    "glm-5.1":           ("GLM 5.1", "ollama"),
    "kimi-k2.6":         ("Kimi K2.6", "ollama"),
    "minimax-m2.7":      ("MiniMax M2.7", "ollama"),
    "deepseek-v4-flash": ("DeepSeek V4", "ollama"),
}

PLANNER_REVIEWER_MODELS: dict[str, str] = {
    "claude-opus-4-7":   "Claude Opus 4.7",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
}


def implementer_provider(tag: str) -> str:
    info = IMPLEMENTER_MODELS.get(tag)
    return info[1] if info else "ollama"


def implementer_display(tag: str) -> str:
    info = IMPLEMENTER_MODELS.get(tag)
    return info[0] if info else tag


def planner_reviewer_display(tag: str) -> str:
    return PLANNER_REVIEWER_MODELS.get(tag, tag)
