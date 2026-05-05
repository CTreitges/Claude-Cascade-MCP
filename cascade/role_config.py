"""Per-Role-Resolution: bündelt für eine Rolle (Plan/Implement/Review/Sub-Agent)
die Wahl von Harness × Provider × Modell × Effort × Sub-Agent-Toggle.

Quellen, in dieser Priorität (höhere überschreibt niedrigere):
  1. Defaults aus cascade.config.Settings (cascade_<role>_model + ggf. effort)
  2. Per-Chat-Overrides aus chat_session-Record:
     - existierende Spalten {role}_model / {role}_effort  (legacy Felder)
     - JSON-Spalte role_overrides_json mit Pro-Rolle dict für harness/provider/enable_subagents
  3. Per-Run-Overrides die der Caller via run_cascade(planner_model=..., ...) reinreicht

Die resolved RoleConfig wird in eine HarnessRequest gemappt und an die jeweilige
Harness übergeben.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field

from cascade.harness.base import HarnessName, ProviderName, RoleName


# ──────────────────────────────────────────────────────────────────────────────
#  Provider-Auto-Detection
# ──────────────────────────────────────────────────────────────────────────────
def detect_provider(model: str) -> ProviderName:
    """Leitet den Provider aus dem Modell-Tag ab.

    - claude-*           → anthropic
    - gpt-*, o1-*, o3-*, o4-* → openai
    - alles andere       → ollama (Default für Open-Weight via Ollama-Cloud)
    """
    m = (model or "").lower()
    if m.startswith("claude-"):
        return "anthropic"
    if m.startswith(("gpt-", "o1-", "o3-", "o4-")):
        return "openai"
    return "ollama"


# ──────────────────────────────────────────────────────────────────────────────
#  RoleConfig
# ──────────────────────────────────────────────────────────────────────────────
class RoleConfig(BaseModel):
    """Aufgelöste Konfig für eine konkrete Rolle in einem Run."""

    role: RoleName
    harness: HarnessName = "claude-code"
    provider: ProviderName = "anthropic"
    model: str = "claude-sonnet-4-6"
    effort: Optional[str] = None
    enable_subagents: bool = False
    max_turns: int = 20
    # Plan v5 R1 — Failover-Chain für diese Rolle. Wenn primary fail't,
    # versuche fallback in Reihenfolge. None = nutze role-default-chain.
    # Format: list of "provider:model" strings, z.B.
    #   ["anthropic:claude-haiku-4-5", "ollama:kimi-k2.6"]
    failover_chain: Optional[list[str]] = None

    def to_harness_request_kwargs(self) -> Dict[str, Any]:
        """Felder die direkt in HarnessRequest passen."""
        return {
            "role": self.role,
            "harness": self.harness,
            "provider": self.provider,
            "model": self.model,
            "max_turns": self.max_turns,
            "enable_subagents": self.enable_subagents,
        }


# ──────────────────────────────────────────────────────────────────────────────
#  Resolution
# ──────────────────────────────────────────────────────────────────────────────
_LEGACY_MODEL_FIELD = {
    "planner":     "cascade_planner_model",
    "implementer": "cascade_implementer_model",
    "reviewer":    "cascade_reviewer_model",
    "triage":      "cascade_triage_model",
    "subagent":    "cascade_implementer_model",   # default: Sub-Agent nutzt Implementer-Modell
    "quick-review": "cascade_reviewer_model",
}

_LEGACY_EFFORT_FIELD = {
    "planner":     "cascade_planner_effort",
    "implementer": "cascade_implementer_effort",
    "reviewer":    "cascade_reviewer_effort",
    "triage":      "cascade_triage_effort",
    "subagent":    "cascade_implementer_effort",
    "quick-review": "cascade_reviewer_effort",
}

_SESSION_MODEL_FIELD = {
    "planner":     "planner_model",
    "implementer": "implementer_model",
    "reviewer":    "reviewer_model",
    "triage":      None,        # triage hat keine eigene session-Spalte
    "subagent":    "implementer_model",
    "quick-review": "reviewer_model",
}

_SESSION_EFFORT_FIELD = {
    "planner":     "planner_effort",
    "implementer": "implementer_effort",
    "reviewer":    "reviewer_effort",
    "triage":      "triage_effort",
    "subagent":    "implementer_effort",
    "quick-review": "reviewer_effort",
}


def parse_role_overrides(raw: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Liest die JSON-Spalte role_overrides_json. Defensiv: ungültiges JSON → {}."""
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def get_role_config(
    role: RoleName,
    settings: Any,
    chat_session: Optional[Dict[str, Any]] = None,
    run_overrides: Optional[Dict[str, Any]] = None,
) -> RoleConfig:
    """Resolved RoleConfig für eine konkrete Rolle in einem Run.

    Priorität: run_overrides > chat_session.role_overrides_json > chat_session legacy
    columns > settings legacy fields > defaults.
    """
    run_overrides = run_overrides or {}
    chat_session = chat_session or {}

    # 1) Defaults aus Settings
    model = getattr(settings, _LEGACY_MODEL_FIELD[role], "claude-sonnet-4-6") or "claude-sonnet-4-6"
    effort = getattr(settings, _LEGACY_EFFORT_FIELD[role], "") or None

    # 2) Per-Chat legacy Spalten
    sess_model_field = _SESSION_MODEL_FIELD.get(role)
    if sess_model_field and chat_session.get(sess_model_field):
        model = chat_session[sess_model_field]
    sess_effort_field = _SESSION_EFFORT_FIELD.get(role)
    if sess_effort_field and chat_session.get(sess_effort_field):
        effort = chat_session[sess_effort_field]

    # 3) Per-Chat JSON role_overrides_json (Quelle für harness/provider/enable_subagents)
    role_ovr = parse_role_overrides(chat_session.get("role_overrides_json")).get(role, {})
    harness = role_ovr.get("harness", "claude-code")
    provider = role_ovr.get("provider")  # None → auto-detect
    enable_subagents = bool(role_ovr.get("enable_subagents", False))
    max_turns = int(role_ovr.get("max_turns", 20))
    if "model" in role_ovr:
        model = role_ovr["model"]
    if "effort" in role_ovr:
        effort = role_ovr["effort"]

    # 4) Run-Time-Overrides (höchste Priorität)
    if "harness" in run_overrides:
        harness = run_overrides["harness"]
    if "provider" in run_overrides:
        provider = run_overrides["provider"]
    if "model" in run_overrides:
        model = run_overrides["model"]
    if "effort" in run_overrides:
        effort = run_overrides["effort"]
    if "enable_subagents" in run_overrides:
        enable_subagents = bool(run_overrides["enable_subagents"])
    if "max_turns" in run_overrides:
        max_turns = int(run_overrides["max_turns"])

    # 5) Provider-Auto-Detection wenn nicht gesetzt
    if not provider:
        provider = detect_provider(model)

    # Failover-Chain Resolution: per-role-default falls nicht gesetzt
    failover_chain = role_ovr.get("failover_chain") or run_overrides.get("failover_chain")
    if failover_chain is None:
        failover_chain = _DEFAULT_FAILOVER_CHAINS.get(role, [])

    return RoleConfig(
        role=role,
        harness=harness,
        provider=provider,
        model=model,
        effort=effort,
        enable_subagents=enable_subagents,
        max_turns=max_turns,
        failover_chain=failover_chain,
    )


# Plan v5 R1 — Default Failover-Chains pro Rolle.
# Format: "provider:model". Primary kommt aus model+provider Auflösung,
# diese Chain ist NACH dem primary. Beim Match übersprungen.
_DEFAULT_FAILOVER_CHAINS: Dict[str, list[str]] = {
    "implementer": [
        "anthropic:claude-sonnet-4-6",
        "ollama:kimi-k2.6",
        "anthropic:claude-haiku-4-5",
    ],
    "planner": [
        "anthropic:claude-opus-4-7",
        "anthropic:claude-sonnet-4-6",
    ],
    "reviewer": [
        "anthropic:claude-haiku-4-5",
        "anthropic:claude-sonnet-4-6",
    ],
    "triage": [
        "anthropic:claude-haiku-4-5",
    ],
    "subagent": [
        "ollama:kimi-k2.6",
        "anthropic:claude-haiku-4-5",
    ],
    "quick-review": [
        "anthropic:claude-haiku-4-5",
    ],
}


def encode_role_overrides(overrides: Dict[str, Dict[str, Any]]) -> str:
    """Inverse von parse_role_overrides — für DB-Schreibzugriff."""
    cleaned = {role: {k: v for k, v in cfg.items() if v is not None}
               for role, cfg in overrides.items()
               if cfg}
    return json.dumps(cleaned, ensure_ascii=False, sort_keys=True) if cleaned else ""
