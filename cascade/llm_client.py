"""Cloud LLM client for the implementer.

Two backends:
  - "ollama"             → ollama.AsyncClient pointing at Ollama Cloud
  - "openai_compatible"  → openai.AsyncOpenAI with a per-provider base_url

Selection lives in `Settings.cascade_implementer_provider`; the model name
(`Settings.cascade_implementer_model`) plus model-prefix routing in
`Settings.openai_compat_credentials()` decides the concrete endpoint for
OpenAI-compatible providers.

Both backends return a single string of JSON-shaped text — the caller is
expected to feed it through `cascade.claude_cli.parse_json_payload`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .config import Settings, settings


@dataclass
class LLMReply:
    text: str
    model: str
    provider: str
    usage: dict[str, Any] | None = None


class LLMClientError(RuntimeError):
    pass


def _build_messages(
    *,
    system: str,
    user: str,
    json_schema_hint: str | None,
) -> list[dict[str, str]]:
    sys_full = system
    if json_schema_hint:
        sys_full = (
            f"{system}\n\n"
            "OUTPUT FORMAT — RESPOND WITH A SINGLE JSON OBJECT MATCHING THIS SCHEMA, "
            "NOTHING ELSE (no prose, no markdown fences):\n"
            f"{json_schema_hint}"
        )
    return [
        {"role": "system", "content": sys_full},
        {"role": "user", "content": user},
    ]


async def implementer_chat(
    *,
    system: str,
    user: str,
    json_schema_hint: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    s: Settings | None = None,
    timeout_s: float = 600,
) -> LLMReply:
    s = s or settings()
    provider = provider or s.cascade_implementer_provider
    model = model or s.cascade_implementer_model
    messages = _build_messages(system=system, user=user, json_schema_hint=json_schema_hint)

    if provider == "ollama":
        return await _ollama_chat(model=model, messages=messages, s=s, timeout_s=timeout_s)
    if provider == "openai_compatible":
        return await _openai_compat_chat(model=model, messages=messages, s=s, timeout_s=timeout_s)
    raise LLMClientError(f"Unknown implementer provider: {provider!r}")


async def _ollama_chat(
    *, model: str, messages: list[dict[str, str]], s: Settings, timeout_s: float
) -> LLMReply:
    import ollama

    from .models import implementer_ctx

    headers: dict[str, str] = {}
    if s.ollama_cloud_api_key:
        headers["Authorization"] = f"Bearer {s.ollama_cloud_api_key}"

    # num_ctx: target the model's max context window. Ollama clamps it
    # automatically if we over-shoot. Big context matters for the implementer
    # because we feed it FULL existing-file contents + plan + reviewer feedback.
    options = {
        "temperature": 0.2,
        "num_ctx": implementer_ctx(model),
    }

    client = ollama.AsyncClient(host=s.ollama_cloud_host, headers=headers, timeout=timeout_s)
    try:
        resp = await client.chat(
            model=model,
            messages=messages,
            format="json",
            options=options,
        )
    except Exception as e:
        raise LLMClientError(f"Ollama Cloud call failed: {e}") from e

    text = (resp.get("message") or {}).get("content", "")
    if not isinstance(text, str) or not text.strip():
        raise LLMClientError(f"Ollama Cloud returned empty content: {resp!r}")
    usage = {
        "prompt_eval_count": resp.get("prompt_eval_count"),
        "eval_count": resp.get("eval_count"),
        "total_duration": resp.get("total_duration"),
    }
    return LLMReply(text=text, model=model, provider="ollama", usage=usage)


async def _openai_compat_chat(
    *, model: str, messages: list[dict[str, str]], s: Settings, timeout_s: float
) -> LLMReply:
    from openai import AsyncOpenAI

    base_url, api_key = s.openai_compat_credentials(model)
    if not api_key:
        raise LLMClientError(
            f"No API key configured for provider matching model {model!r} "
            f"(expected one of GLM_API_KEY / DEEPSEEK_API_KEY / MINIMAX_API_KEY / KIMI_API_KEY)"
        )

    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout_s)
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,  # type: ignore[arg-type]
            temperature=0.2,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        raise LLMClientError(f"OpenAI-compatible call ({model}@{base_url}) failed: {e}") from e

    if not resp.choices:
        raise LLMClientError(f"OpenAI-compatible response had no choices: {resp!r}")
    text = resp.choices[0].message.content or ""
    if not text.strip():
        raise LLMClientError(f"OpenAI-compatible response had empty content: {resp!r}")
    usage = None
    if resp.usage:
        usage = {
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
            "total_tokens": resp.usage.total_tokens,
        }
    return LLMReply(text=text, model=model, provider="openai_compatible", usage=usage)


# ---------- helpers ----------


def schema_as_hint(schema: dict[str, Any]) -> str:
    """Render a tiny JSON schema as a hint string for the system prompt."""
    return json.dumps(schema, indent=2, ensure_ascii=False)
