from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_env_files() -> tuple[str, ...]:
    """Layer multiple env files. Pydantic loads them in order with later
    files taking precedence over earlier — so we put the user-editable
    `.env` first and the wizard-managed `secrets.env` last.

    `secrets.env` is gitignored (see `.gitignore`) and is the file the
    `/setup` wizard writes to. That way the user's hand-edited `.env`
    is never overwritten by the wizard.

    The secrets file lives at `<CASCADE_HOME>/secrets.env` (auto-created
    by the wizard); we ALSO accept a top-level `secrets.env` for users
    who prefer their secrets in the repo dir during local dev.
    """
    out = [".env"]
    home = os.environ.get("CASCADE_HOME") or str(Path.home() / "claude-cascade")
    candidates = [
        Path("secrets.env"),                      # repo-root override
        Path(home) / "secrets.env",               # default secrets path
    ]
    for c in candidates:
        try:
            if c.is_file():
                out.append(str(c))
        except Exception:
            continue
    return tuple(out)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_resolve_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    telegram_bot_token: str = ""
    telegram_owner_id: int = 0
    cascade_bot_lang: Literal["de", "en"] = "de"

    @field_validator("telegram_owner_id", mode="before")
    @classmethod
    def _empty_owner_id_is_zero(cls, v):
        if v in ("", None):
            return 0
        return v

    cascade_implementer_provider: Literal["ollama", "openai_compatible"] = "ollama"
    # 2026-04-27: Switched default from qwen3-coder:480b to kimi-k2.6.
    # SWE-bench Verified ranking April 2026 puts kimi-k2.6 at 80.2%
    # (top open-source spot), with near-doubled tool-calling reliability
    # vs k2.5 — exactly the JSON-strict implementer profile cascade needs.
    # Both available via Ollama Cloud, same API path. Override per-run via
    # /models in the bot or pass implementer_model= to run_cascade.
    cascade_implementer_model: str = "kimi-k2.6"
    cascade_implementer_tools: Literal["fileops", "mcp"] = "fileops"

    ollama_cloud_host: str = "https://ollama.com"
    ollama_cloud_api_key: str = ""

    glm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    glm_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_api_key: str = ""
    minimax_base_url: str = "https://api.minimaxi.chat/v1"
    minimax_api_key: str = ""
    kimi_base_url: str = "https://api.moonshot.cn/v1"
    kimi_api_key: str = ""

    openai_api_key: str = ""

    # Brave Search (Web). Enable by setting BRAVE_SEARCH_API_KEY in .env;
    # without a key the cascade still works, just without live web results.
    brave_search_api_key: str = ""

    # On-demand external context fetching. cascade.research.gather_external_context
    # consults these flags before reaching out.
    cascade_context7_enabled: bool = True
    cascade_websearch_enabled: bool = True

    # When the bot starts and finds tasks in 'running' state (= the previous
    # process was killed mid-run), automatically resume them after a grace
    # period. Disable by setting CASCADE_AUTO_RESUME_INTERRUPTED=false.
    cascade_auto_resume_interrupted: bool = True

    # When the planner thinks a task is multi-component, let it emit
    # `subtasks` and have the supervisor run them sequentially on the shared
    # workspace. Set to False to force single-shot mode.
    cascade_auto_decompose: bool = True
    # Hard ceiling on how many sub-tasks the planner may emit. The planner
    # gets told this number; if it exceeds, supervisor truncates.
    cascade_max_subtasks: int = 6

    cascade_planner_model: str = "claude-opus-4-7"
    cascade_reviewer_model: str = "claude-sonnet-4-6"
    cascade_triage_model: str = "claude-sonnet-4-6"
    cascade_triage_enabled: bool = True
    # Effort levels passed via `claude -p --effort <level>`. Allowed: low,
    # medium, high, xhigh, max. Empty string → omit the flag (use Claude's default).
    cascade_planner_effort: str = ""
    cascade_reviewer_effort: str = ""
    cascade_triage_effort: str = ""
    # Only honored when the implementer model is a Claude tag; silently
    # dropped for Ollama models which don't have an effort knob.
    cascade_implementer_effort: str = ""

    cascade_home: Path = Field(default_factory=lambda: Path.home() / "claude-cascade")
    cascade_timezone: str = "Europe/Berlin"
    # Iteration cap — set to UNLIMITED_SENTINEL (999) by default per user
    # decision: only LLM-usage / rate-limit budget should stop a run, not an
    # arbitrary count. The per-chat /iterations command can still pin a
    # tighter cap for debugging. Stagnation-detection in core.py prevents
    # endless identical-diff loops independent of this number.
    cascade_max_iterations: int = 999
    # When the implementer-reviewer loop gets stuck (same check/feedback failing
    # repeatedly), invoke the planner again with the failure history so it can
    # rewrite the plan and quality_checks. Configurable via /failsbeforereplan.
    cascade_replan_after_failures: int = 2
    # Hard ceiling on how many full replan rounds a single run may trigger —
    # this DOES stay capped (different from iterations) because each replan
    # restarts the planner LLM call, which is the expensive part. Configurable
    # via /replan.
    cascade_replan_max: int = 2
    # When True, after every successful run the planner is asked whether
    # the task pattern is worth saving as a reusable skill. Suggestions
    # are surfaced to the bot owner via inline-keyboard.
    cascade_auto_skill_suggest: bool = True
    cascade_skill_suggest_cooldown_s: int = 300
    cascade_workspace_retention_days: int = 7
    # P2.3: hard cap on how long with_retry waits when an upstream LLM
    # is failing. The default 7 days lets cascade-internal calls survive
    # cloud outages, but for interactive runs that's overkill — the user
    # would rather see a clean fail than a week-long "still trying" message.
    # Override per run via run_cascade(..., max_wait_s=...).
    cascade_max_wait_s: int = 7 * 86400
    # P2.4: workspace size quota — abort iteration if cumulative diff
    # plus tracked files exceed this. Catches runaway implementer
    # writes (1GB log files, infinite loops in generated code) before
    # they fill the disk. 0 disables.
    cascade_workspace_max_bytes: int = 1_073_741_824  # 1 GB
    cascade_db_path: Path = Field(
        default_factory=lambda: Path.home() / "claude-cascade" / "store" / "cascade.db"
    )
    # Verbose logging — when True, every layer logs at DEBUG and a rotating
    # file handler under store/debug.log captures the firehose. Used during
    # post-mortem of stuck cascades or weird triage decisions.
    cascade_debug: bool = False
    # Background chat-summariser: condenses old messages into chat_summaries
    # rows so build_context() can carry long-term anchors without dumping
    # raw history into every prompt. Disable to keep messages verbatim.
    cascade_summarize_enabled: bool = True
    cascade_summarize_tick_s: int = 6 * 3600
    # Multi-plan voting: produce 2 plans in parallel (different temperatures)
    # and let a Sonnet picker choose the better one before the loop starts.
    # Doubles planner cost — off by default. Per-chat /multiplan toggle.
    cascade_multiplan_enabled: bool = False

    @property
    def workspaces_dir(self) -> Path:
        return self.cascade_home / "workspaces"

    def openai_compat_credentials(self, model: str) -> tuple[str, str]:
        """Map a model name prefix to (base_url, api_key) for OpenAI-compatible providers."""
        m = model.lower()
        if m.startswith("glm"):
            return self.glm_base_url, self.glm_api_key
        if m.startswith("deepseek"):
            return self.deepseek_base_url, self.deepseek_api_key
        if m.startswith("minimax") or m.startswith("abab"):
            return self.minimax_base_url, self.minimax_api_key
        if m.startswith("kimi") or m.startswith("moonshot"):
            return self.kimi_base_url, self.kimi_api_key
        raise ValueError(f"No OpenAI-compatible credentials configured for model {model!r}")


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
