"""Curated catalog of supported models for each worker.

Tags verified against Ollama Cloud's /v1/models response (2026-04-25).
Add or rename entries here; the bot's /models command picks them up automatically.
"""

from __future__ import annotations

# tag → (display name, provider)
IMPLEMENTER_MODELS: dict[str, tuple[str, str]] = {
    "qwen3-coder:480b":    ("Qwen3 Coder 480B (Default)", "ollama"),
    "glm-5.1":             ("GLM 5.1", "ollama"),
    "glm-5":               ("GLM 5", "ollama"),
    "glm-4.7":             ("GLM 4.7", "ollama"),
    "minimax-m2.7":        ("MiniMax M2.7", "ollama"),
    "minimax-m2.5":        ("MiniMax M2.5", "ollama"),
    "deepseek-v4-flash":   ("DeepSeek V4 Flash", "ollama"),
    "deepseek-v3.2":       ("DeepSeek V3.2", "ollama"),
    "deepseek-v3.1:671b":  ("DeepSeek V3.1 671B", "ollama"),
    "kimi-k2.6":           ("Kimi K2.6", "ollama"),
    "kimi-k2-thinking":    ("Kimi K2 Thinking", "ollama"),
    "kimi-k2:1t":          ("Kimi K2 1T", "ollama"),
}

PLANNER_REVIEWER_MODELS: dict[str, str] = {
    "claude-opus-4-7":   "Claude Opus 4.7",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "claude-haiku-4-5":  "Claude Haiku 4.5",
}


def implementer_provider(tag: str) -> str:
    info = IMPLEMENTER_MODELS.get(tag)
    return info[1] if info else "ollama"


def implementer_display(tag: str) -> str:
    info = IMPLEMENTER_MODELS.get(tag)
    return info[0] if info else tag


def planner_reviewer_display(tag: str) -> str:
    return PLANNER_REVIEWER_MODELS.get(tag, tag)
