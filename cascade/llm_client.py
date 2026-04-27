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
    effort: str | None = None,
    temperature: float | None = None,
    s: Settings | None = None,
    timeout_s: float = 1800,
) -> LLMReply:
    s = s or settings()
    provider = provider or s.cascade_implementer_provider
    model = model or s.cascade_implementer_model
    messages = _build_messages(system=system, user=user, json_schema_hint=json_schema_hint)
    from .rate_limit import with_retry

    async def _call():
        if provider == "ollama":
            return await _ollama_chat(
                model=model, messages=messages, s=s, timeout_s=timeout_s, temperature=temperature,
            )
        if provider == "openai_compatible":
            return await _openai_compat_chat(
                model=model, messages=messages, s=s, timeout_s=timeout_s,
            )
        if provider == "claude":
            return await _claude_chat(
                model=model, messages=messages, s=s, timeout_s=timeout_s, effort=effort,
            )
        raise LLMClientError(f"Unknown implementer provider: {provider!r}")

    return await with_retry(
        _call,
        label=f"implementer/{provider}",
        max_total_wait_s=float(s.cascade_max_wait_s),
    )


async def _claude_chat(
    *, model: str, messages: list[dict[str, str]], s: Settings,
    timeout_s: float, effort: str | None = None,
) -> LLMReply:
    """Run the Implementer via the local `claude` CLI (Max-Subscription auth).
    Useful when the user picks Sonnet or Opus as the implementer model."""
    from .claude_cli import ClaudeCliError, claude_call

    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    user = next((m["content"] for m in messages if m["role"] == "user"), "")
    try:
        result = await claude_call(
            prompt=user,
            model=model,
            system_prompt=system,
            output_json=True,
            effort=effort,
            timeout_s=int(timeout_s),
        )
    except ClaudeCliError as e:
        raise LLMClientError(f"claude implementer call failed: {e}") from e
    return LLMReply(text=result.text, model=model, provider="claude", usage=None)


async def agent_chat(
    *,
    prompt: str,
    model: str,
    system_prompt: str,
    output_json: bool = True,
    effort: str | None = None,
    temperature: float | None = None,
    attachments: list | None = None,
    timeout_s: float = 1800,
    s: Settings | None = None,
    retry_max_total_wait_s: float | None = None,
    retry_min_backoff_s: float | None = None,
    retry_max_backoff_s: float | None = None,
) -> str:
    """Provider-aware single-shot LLM call for Planner / Reviewer / Triage.
    Returns the raw text reply (caller parses JSON itself).

    Routes by model tag: Claude tags → `claude_cli.claude_call()`, everything
    else → Ollama Cloud. `effort` and `attachments` are Claude-only — silently
    ignored on the Ollama path (with a debug log).

    Retry tuning: pass `retry_max_total_wait_s` to cap total wait (default
    12h is meant for cascade-internal calls; UX-facing triage should use
    something tighter like 180s). `retry_min_backoff_s` (default 30s)
    controls the first-retry sleep — triage often wants 10s here.
    """
    s = s or settings()
    is_claude = model.startswith("claude-")
    from .rate_limit import with_retry

    retry_kwargs: dict = {}
    # P2.3: default to settings.cascade_max_wait_s instead of with_retry's
    # 7-day hardcode. Caller can override (triage uses 180s for UX).
    retry_kwargs["max_total_wait_s"] = (
        float(retry_max_total_wait_s)
        if retry_max_total_wait_s is not None
        else float(s.cascade_max_wait_s)
    )
    if retry_min_backoff_s is not None:
        retry_kwargs["min_backoff_s"] = retry_min_backoff_s
    if retry_max_backoff_s is not None:
        retry_kwargs["max_backoff_s"] = retry_max_backoff_s

    if is_claude:
        from .claude_cli import ClaudeCliError, claude_call

        async def _claude():
            try:
                result = await claude_call(
                    prompt=prompt,
                    model=model,
                    system_prompt=system_prompt,
                    output_json=output_json,
                    effort=effort,
                    attachments=attachments,
                    timeout_s=int(timeout_s),
                )
            except ClaudeCliError as e:
                raise LLMClientError(f"claude agent call failed: {e}") from e
            return result.text

        return await with_retry(_claude, label=f"agent/{model}", **retry_kwargs)

    if attachments:
        import logging
        logging.getLogger("cascade.llm_client").debug(
            "agent_chat: dropping %d attachment(s) — model %r is not vision-capable in this code path",
            len(attachments), model,
        )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    async def _ollama():
        reply = await _ollama_chat(
            model=model, messages=messages, s=s, timeout_s=timeout_s, temperature=temperature,
        )
        return reply.text

    return await with_retry(_ollama, label=f"agent/{model}", **retry_kwargs)


async def _ollama_chat(
    *, model: str, messages: list[dict[str, str]], s: Settings, timeout_s: float,
    temperature: float | None = None,
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
        "temperature": 0.2 if temperature is None else float(temperature),
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
        from .rate_limit import RateLimitError, parse_retry_after
        # Build a richer error fingerprint than just str(e). Ollama's
        # ResponseError occasionally has empty str() but a populated
        # status_code attribute — without status info, our short-
        # backoff classifier in rate_limit.py can't tell a 500 from
        # a real 429, so we'd default to a 1h wait. Mining type-name
        # and status_code makes the message classifiable.
        parts = []
        type_name = type(e).__name__
        if type_name and type_name != "Exception":
            parts.append(type_name)
        status = getattr(e, "status_code", None)
        if status:
            parts.append(f"status code: {status}")
        body = str(e)
        if body:
            parts.append(body[:300])
        diag = " — ".join(parts) if parts else f"{type_name} (no detail)"
        # User-explicit policy (2026-04-27): ALL Ollama Cloud errors are
        # treated as transient — 5xx, 4xx, network drops, timeouts, weird
        # client errors. with_retry waits and tries again until the service
        # recovers. Permanent config errors (no API key) are raised earlier
        # and never reach this except.
        raise RateLimitError(
            f"Ollama Cloud error (will retry): {diag}",
            retry_after=parse_retry_after(diag),
        ) from e

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
        from .rate_limit import RateLimitError, parse_retry_after
        msg = str(e)
        # User-explicit policy: all cloud-LLM errors retryable (see
        # _ollama_cloud_chat). Permanent config errors are raised before this.
        raise RateLimitError(
            f"OpenAI-compatible call error (will retry) ({model}@{base_url}): {msg[:400]}",
            retry_after=parse_retry_after(msg),
        ) from e

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
