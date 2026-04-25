from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
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
    cascade_implementer_model: str = "qwen3-coder:480b"
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

    cascade_planner_model: str = "claude-opus-4-7"
    cascade_reviewer_model: str = "claude-sonnet-4-6"
    cascade_triage_model: str = "claude-sonnet-4-6"
    cascade_triage_enabled: bool = True

    cascade_home: Path = Field(default_factory=lambda: Path.home() / "claude-cascade")
    cascade_max_iterations: int = 3
    cascade_workspace_retention_days: int = 7
    cascade_db_path: Path = Field(
        default_factory=lambda: Path.home() / "claude-cascade" / "store" / "cascade.db"
    )

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
