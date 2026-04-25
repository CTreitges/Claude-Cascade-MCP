"""Headless `claude -p` subprocess wrapper for the planner and reviewer.

Default uses normal auth (OAuth from keychain). Set bare=True if
ANTHROPIC_API_KEY is exported — that path skips CLAUDE.md auto-discovery,
hooks, and keychain, which is faster but requires explicit credentials.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path


class ClaudeCliError(RuntimeError):
    pass


@dataclass
class ClaudeResult:
    text: str
    raw: dict | None  # parsed JSON envelope when --output-format=json
    duration_s: float
    cost_usd: float | None = None


def _attachments_block(attachments: list[Path]) -> str:
    if not attachments:
        return ""
    lines = ["", "Attachments:"]
    for p in attachments:
        lines.append(f"@{p}")
    return "\n".join(lines)


async def claude_call(
    *,
    prompt: str,
    model: str,
    system_prompt: str | None = None,
    attachments: list[Path] | None = None,
    output_json: bool = True,
    timeout_s: float = 600,
    bare: bool = False,
    extra_flags: list[str] | None = None,
    effort: str | None = None,
) -> ClaudeResult:
    """Invoke `claude -p` and return the result.

    With `output_json=True`, parses the result envelope and returns the assistant text
    in `.text` and the full envelope in `.raw`.
    """
    args: list[str] = ["claude", "-p"]
    if bare:
        args.append("--bare")
    if output_json:
        args += ["--output-format", "json"]
    args += ["--model", model]
    if effort:
        args += ["--effort", effort]
    if system_prompt:
        args += ["--system-prompt", system_prompt]
    if extra_flags:
        args += extra_flags

    full_prompt = prompt + _attachments_block(attachments or [])
    args.append(full_prompt)

    # `CLAUDE_CODE_SIMPLE=1` is the env equivalent of `--bare` and blocks keychain auth.
    # Only set it when bare=True; otherwise keep the inherited environment intact so OAuth works.
    env = {**os.environ}
    if bare:
        env["CLAUDE_CODE_SIMPLE"] = "1"
    else:
        env.pop("CLAUDE_CODE_SIMPLE", None)

    started = asyncio.get_event_loop().time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise ClaudeCliError(f"claude -p timed out after {timeout_s}s")
    except FileNotFoundError as e:
        raise ClaudeCliError(f"`claude` CLI not found: {e}") from e

    duration = asyncio.get_event_loop().time() - started
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        raise ClaudeCliError(
            f"claude -p exited {proc.returncode}: {stderr.strip() or stdout.strip()}\n"
            f"args: {shlex.join(args)}"
        )

    if not output_json:
        return ClaudeResult(text=stdout.strip(), raw=None, duration_s=duration)

    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ClaudeCliError(f"claude -p produced invalid JSON: {e}\n--- stdout ---\n{stdout}") from e

    text = (
        envelope.get("result")
        or envelope.get("text")
        or envelope.get("response")
        or ""
    )
    cost = envelope.get("total_cost_usd") or envelope.get("cost_usd")
    return ClaudeResult(text=str(text), raw=envelope, duration_s=duration, cost_usd=cost)


def parse_json_payload(text: str) -> dict:
    """Pull a JSON object out of a claude response that may include fences/prose."""
    text = text.strip()
    # Strip ```json … ``` fences if present.
    if text.startswith("```"):
        # find first newline (after ```json or just ```)
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1 :]
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # try to find the first {...} balanced block
        start = text.find("{")
        if start == -1:
            raise ClaudeCliError(f"no JSON object found in response: {text[:200]!r}")
        depth = 0
        for i, ch in enumerate(text[start:], start=start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    snippet = text[start : i + 1]
                    return json.loads(snippet)
        raise ClaudeCliError(f"unbalanced JSON in response: {text[:200]!r}")
