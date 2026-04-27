"""Curated catalog of supported models for each worker.

Tags verified against Ollama Cloud's /v1/models response (2026-04-25).
Add or rename entries here; the bot's /models command picks them up automatically.
"""

from __future__ import annotations

# tag → (display name, provider)
# Curated user-facing list shown in the Telegram /models menu.
# kimi-k2.6 is the runtime default (CASCADE_IMPLEMENTER_MODEL) since
# 2026-04-27 — see config.py:60 for rationale (SWE-bench leadership +
# top-tier tool-calling reliability). The menu still lists multiple
# cloud options so the user can switch with /models.
IMPLEMENTER_MODELS: dict[str, tuple[str, str]] = {
    "qwen3-coder:480b":  ("Qwen3 Coder 480B", "ollama"),
    "qwen3.5:397b":      ("Qwen 3.5 397B (neueste Gen)", "ollama"),
    "glm-5.1":           ("GLM 5.1", "ollama"),
    "kimi-k2.6":         ("Kimi K2.6", "ollama"),
    "minimax-m2.7":      ("MiniMax M2.7", "ollama"),
    "deepseek-v4-flash": ("DeepSeek V4 Flash", "ollama"),
    "claude-sonnet-4-6": ("Claude Sonnet 4.6", "claude"),
    "claude-opus-4-7":   ("Claude Opus 4.7", "claude"),
}

# Per-model context-window targets. Ollama clamps to the actual model-max
# automatically, so over-shooting is safe; the goal is "as much as the model
# physically supports". Verified Apr 2026 against each model's vendor docs.
IMPLEMENTER_CTX: dict[str, int] = {
    "qwen3-coder:480b":  256_000,
    "qwen3.5:397b":      256_000,
    "glm-5.1":           200_000,
    "minimax-m2.7":      256_000,
    "deepseek-v4-flash": 128_000,
    "kimi-k2.6":         256_000,
    # generous default for any newer / unlisted tag
}
DEFAULT_IMPLEMENTER_CTX = 200_000


PLANNER_REVIEWER_MODELS: dict[str, str] = {
    "claude-opus-4-7":   "Claude Opus 4.7",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
    # Ollama Cloud alternatives (cheap, no Max-subscription burn)
    "qwen3-coder:480b":  "Qwen3 Coder 480B",
    "qwen3.5:397b":      "Qwen 3.5 397B",
    "glm-5.1":           "GLM 5.1",
    "kimi-k2.6":         "Kimi K2.6",
    "minimax-m2.7":      "MiniMax M2.7",
    "deepseek-v4-flash": "DeepSeek V4 Flash",
}


def implementer_provider(tag: str) -> str:
    info = IMPLEMENTER_MODELS.get(tag)
    return info[1] if info else "ollama"


def implementer_display(tag: str) -> str:
    info = IMPLEMENTER_MODELS.get(tag)
    return info[0] if info else tag


def implementer_ctx(tag: str) -> int:
    """Return the target num_ctx for an Ollama Cloud implementer model.

    Ollama silently clamps to the model's actual max, so any value here is
    a target / upper bound. Falls back to DEFAULT_IMPLEMENTER_CTX for
    unlisted tags.
    """
    return IMPLEMENTER_CTX.get(tag, DEFAULT_IMPLEMENTER_CTX)


CHAT_MODELS: dict[str, str] = {
    "claude-haiku-4-5":  "Claude Haiku 4.5",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "claude-opus-4-7":   "Claude Opus 4.7",
    # Ollama Cloud alternatives — cheap dispatcher / casual chat
    "qwen3-coder:480b":  "Qwen3 Coder 480B",
    "qwen3.5:397b":      "Qwen 3.5 397B",
    "glm-5.1":           "GLM 5.1",
    "kimi-k2.6":         "Kimi K2.6",
    "minimax-m2.7":      "MiniMax M2.7",
    "deepseek-v4-flash": "DeepSeek V4 Flash",
}


def planner_reviewer_display(tag: str) -> str:
    return PLANNER_REVIEWER_MODELS.get(tag, tag)


def chat_display(tag: str) -> str:
    return CHAT_MODELS.get(tag, tag)


# ---- effort capability per model ----------------------------------------

# Effort-level capability per model.
# `effort` is a `claude -p --effort <level>` flag; only Claude models honor it.
# Ollama Cloud has no equivalent — its API exposes temperature/top_p/num_ctx
# but no reasoning-effort knob.
# - Claude Opus/Sonnet: all 5 levels (low/medium/high + extended xhigh/max)
# - Claude Haiku:       low/medium/high only (no extended thinking)
# - Ollama models:      empty tuple → caller should hide effort UI
_CLAUDE_FULL = ("low", "medium", "high", "xhigh", "max")
_CLAUDE_LIGHT = ("low", "medium", "high")

_EFFORT_BY_MODEL: dict[str, tuple[str, ...]] = {
    "claude-opus-4-7":   _CLAUDE_FULL,
    "claude-sonnet-4-6": _CLAUDE_FULL,
    "claude-haiku-4-5":  _CLAUDE_LIGHT,
}


def effort_levels_for(model_tag: str | None) -> tuple[str, ...]:
    """Return the effort levels the given model honors.

    - Claude tag → matching tuple (full or light)
    - Ollama / unknown / None → empty tuple (caller should hide effort UI)
    """
    if not model_tag:
        return ()
    if not model_tag.startswith("claude-"):
        return ()
    return _EFFORT_BY_MODEL.get(model_tag, _CLAUDE_FULL)


def model_supports_effort(model_tag: str | None) -> bool:
    return bool(effort_levels_for(model_tag))
