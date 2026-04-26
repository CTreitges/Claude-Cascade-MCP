"""Identify uploaded files so the bot can do the right thing with them.

When the user sends a file via Telegram (`on_photo_or_document` in
`bot.handlers.messages`), this module returns a small classification
dict the rest of the bot can act on:

  - `kind`            — short symbolic name (e.g. `google_service_account`)
  - `summary`         — one-line human-readable description for the chat log
  - `suggested_target`— absolute path the file would be staged at, or None
  - `auto_stage_safe` — True only when classification is unambiguous AND
                        the suggested target is in `simple_actions._ALLOWED_ROOTS`.
                        The smart-document handler stages those WITHOUT
                        asking the user.

Two entry points:

  - `classify_uploaded_json(text)`  for `application/json` / `*.json`
    uploads — recognises Google Service-Account, Google OAuth client,
    AWS credentials, OpenAI-style `{api_key: ...}`, otherwise falls
    back to `generic_config_json`.

  - `classify_uploaded_text(name, text)`  soft classifier for non-JSON
    text uploads (markdown, requirements.txt, .py source, dotenv KEY=VALUE
    lists). Used so `chat_messages.file_classification` has structure
    even for files we don't auto-stage.

Both functions return None when the input is not parseable / not a
recognisable kind. They never raise.
"""

from __future__ import annotations

import json


def classify_uploaded_json(text: str) -> dict | None:
    """If `text` looks like a recognisable JSON config/credential, return
    a classification dict. Otherwise return None (un-parseable) or a
    `generic_*` fallback."""
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return {
            "kind": "generic_json",
            "summary": "JSON data",
            "suggested_target": None,
            "auto_stage_safe": False,
        }

    # Google Service Account
    if data.get("type") == "service_account" and "client_email" in data:
        project_id = data.get("project_id") or "unknown"
        return {
            "kind": "google_service_account",
            "summary": (
                f"Google Service-Account "
                f"({data.get('client_email', '?')}, "
                f"project={project_id})"
            ),
            "client_email": data.get("client_email"),
            "project_id": project_id,
            "suggested_target": f"~/.config/gcloud/{project_id}-sa.json",
            "auto_stage_safe": True,
        }

    # Google OAuth client (installed/web flow secrets)
    if "installed" in data or "web" in data:
        sub = data.get("installed") or data.get("web") or {}
        if "client_id" in sub and "client_secret" in sub:
            return {
                "kind": "google_oauth_client",
                "summary": "Google OAuth client credentials",
                "suggested_target": "~/.config/gcloud/oauth-client.json",
                "auto_stage_safe": True,
            }

    # AWS credentials — sometimes shipped as JSON via `aws sts ...`
    if (
        ("AccessKeyId" in data and "SecretAccessKey" in data)
        or ("aws_access_key_id" in data and "aws_secret_access_key" in data)
    ):
        return {
            "kind": "aws_credentials",
            "summary": "AWS access-key credentials",
            # Deliberately NOT auto-staging: AWS prefers `~/.aws/credentials`
            # (INI), not a JSON drop. Confirm with user.
            "suggested_target": "~/.aws/credentials.json",
            "auto_stage_safe": False,
        }

    # OpenAI-style {api_key: ...}
    if "api_key" in data and isinstance(data.get("api_key"), str):
        return {
            "kind": "openai_credentials",
            "summary": "OpenAI-style {api_key: ...} credential file",
            "suggested_target": None,  # belongs in .env, not as standalone file
            "auto_stage_safe": False,
        }

    # Catch-all for arbitrary JSON config
    return {
        "kind": "generic_config_json",
        "summary": f"JSON config with keys: {', '.join(list(data.keys())[:6])}",
        "suggested_target": None,
        "auto_stage_safe": False,
    }


def classify_uploaded_text(name: str, text: str) -> dict | None:
    """Soft classification for non-JSON text uploads. Used to set
    `file_classification` on `chat_messages` so the recall layer has
    structure to grep on, even though we don't auto-stage these.

    Detection order matters — first match wins:
      1. by extension: .md/.markdown, .py, .env, .yaml/.yml, .toml
      2. by filename: `requirements.txt`
      3. by shebang / Python-import header
      4. by content shape: KEY=VALUE majority → `dotenv_snippet`
    """
    if not text:
        return None
    name_lower = (name or "").lower()
    head = text.lstrip()[:200]

    # Extension-driven first
    if name_lower.endswith((".md", ".markdown")):
        return {"kind": "markdown_doc", "summary": f"Markdown ({len(text)}B)"}
    if name_lower == "requirements.txt" or name_lower.endswith("/requirements.txt"):
        return {"kind": "requirements_txt", "summary": "Python requirements list"}
    if name_lower.endswith(".py") or head.startswith(
        ("#!/usr/bin/env python", "import ", "from ")
    ):
        return {"kind": "python_script", "summary": f"Python source ({len(text)}B)"}
    if name_lower.endswith(".env") or ".env." in name_lower:
        return {"kind": "dotenv_snippet", "summary": "dotenv KEY=VALUE list"}

    # Heuristic: lines look like KEY=VALUE
    lines = [ln for ln in text.splitlines() if ln.strip()]
    kv_lines = sum(
        1 for ln in lines
        if "=" in ln and not ln.lstrip().startswith("#")
    )
    if lines and kv_lines >= max(1, len(lines) // 2):
        return {
            "kind": "dotenv_snippet",
            "summary": f"KEY=VALUE list ({len(lines)} lines)",
        }

    return None
