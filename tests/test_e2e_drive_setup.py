"""End-to-end-style verification: the Drive-Setup scenario from the
2026-04-26 production log no longer dies of chat-amnesia.

Original failure (logs):
  - 12:46  user uploads google service-account JSON
  - 12:50  bot pre-stages it under ~/.config/scdl/google-sa.json
  - 12:53  user asks "hast du die json?"
           → Bot: "Nein, ich habe keine JSON-Datei erhalten."

Root causes (all addressed in plan resilient-gliding-lobster):
  - chat_messages stored only "[file received] foo.json", not content
  - RLM recall used substring matching on >4-char words → "json" missed
  - Triage system prompt didn't mention file-awareness
  - Pre-stage required ask_user with 5min timeout (often expired)

This test suite walks the same data path WITHOUT spinning up Telegram or
running an LLM, just exercising the building blocks that were broken:

  1. ChatMemory.append() with file_content + classification persists.
  2. ChatMemory.build_context() surfaces the file in CONVERSATION + RECENT
     UPLOADS blocks.
  3. The new BM25 recall finds short keywords like "json" / "drive".
  4. _classify_uploaded_json() produces an auto_stage_safe verdict for
     the matching project_id.
  5. Triage's _validate_direct_action() rejects targets outside the allow-
     list AND accepts the pre-staged path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cascade.bot.handlers.messages import (
    _classify_uploaded_json,
    _classify_uploaded_text,
)
from cascade.chat_memory import ChatMemory, ChatMemoryConfig
from cascade.memory import recall_context, remember_finding
from cascade.simple_actions import is_target_in_allowlist
from cascade.store import Store
from cascade.triage import _validate_direct_action


SA_JSON_LITERAL = json.dumps({
    "type": "service_account",
    "project_id": "soundcloud-downloader-494512",
    "client_email": "ultraclaude@soundcloud-downloader-494512.iam.gserviceaccount.com",
    "private_key_id": "0ffcb619c7d4dummy",
    "private_key": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
})


@pytest.fixture
async def store(tmp_path: Path) -> Store:
    s = await Store.open(tmp_path / "e2e.db")
    yield s
    await s.close()


async def test_chat_memory_remembers_uploaded_json_after_filler(
    store: Store,
):
    """The exact pattern from 12:46 → 12:53: upload, then chat, then ask."""
    chat_id = 123
    cm = ChatMemory(store, config=ChatMemoryConfig(
        limit_hot=30, limit_warm=2, limit_fts=5,
        file_content_chars=500,
    ))
    classification = _classify_uploaded_json(SA_JSON_LITERAL)
    assert classification is not None
    assert classification["kind"] == "google_service_account"
    assert classification["auto_stage_safe"] is True
    assert classification["project_id"] == "soundcloud-downloader-494512"

    # 12:46
    await cm.append(
        chat_id, "user", "hier die json für drive",
        file_path="/home/chris/.config/gcloud/soundcloud-downloader-494512-sa.json",
        file_content=SA_JSON_LITERAL,
        file_classification=classification,
    )
    # 12:50
    await cm.append(
        chat_id, "bot",
        "Datei abgelegt unter ~/.config/gcloud/soundcloud-downloader-494512-sa.json (chmod 600)",
    )
    # 12:51-12:52 filler chat
    for i in range(3):
        await cm.append(chat_id, "user", f"alles klar danke ({i})")

    # 12:53 — the moment the old bot would say "habe keine JSON erhalten"
    ctx = await cm.build_context(chat_id, query="hast du die json?", lang="de")
    assert ctx, "build_context returned nothing"
    # The block must contain enough specific anchors for triage to answer:
    assert "google_service_account" in ctx
    assert "soundcloud-downloader-494512" in ctx
    # The actual file content (or its inlined snippet) must be reachable:
    assert "service_account" in ctx
    # Path must show up so the bot can quote it back to the user:
    assert ".config/gcloud/" in ctx
    # And the recent-uploads block must declare the upload was handled:
    assert "RECENT UPLOADS" in ctx or "KüRZLICH HOCHGELADENE" in ctx


async def test_short_keyword_recall_finds_credential_finding(tmp_path, monkeypatch):
    """The BM25 recall (memory.py) must surface the staged credential
    even when the user types a short query like "json" or "drive"."""
    from cascade.config import Settings
    fake = Settings(cascade_home=tmp_path)
    import cascade.memory as mod
    monkeypatch.setattr(mod, "settings", lambda: fake)

    await remember_finding(
        "credential JSON for project soundcloud-downloader-494512 staged at "
        "/home/chris/.config/gcloud/soundcloud-downloader-494512-sa.json",
        category="fact",
        importance="high",
        tags="claude-cascade,credential,drive",
    )
    # Add a few unrelated entries so we're not testing the trivial case.
    for i in range(5):
        await remember_finding(f"random note {i}", tags="misc")

    out = await recall_context("hast du die drive json")
    assert out is not None
    assert "soundcloud-downloader-494512" in out
    assert "credential" in out.lower()


async def test_classify_extra_kinds_returns_useful_metadata():
    cls_md = _classify_uploaded_text("README.md", "# Hello\n\nworld")
    assert cls_md and cls_md["kind"] == "markdown_doc"

    cls_req = _classify_uploaded_text(
        "requirements.txt", "pydantic==2.5.0\nfastapi>=0.100\n",
    )
    assert cls_req and cls_req["kind"] == "requirements_txt"

    cls_py = _classify_uploaded_text(
        "setup.py", "from setuptools import setup\nsetup(name='x')",
    )
    assert cls_py and cls_py["kind"] == "python_script"

    cls_env = _classify_uploaded_text(
        ".env.example",
        "FOO=1\nBAR=baz\n# a comment\nZAP=qux",
    )
    assert cls_env and cls_env["kind"] == "dotenv_snippet"


def test_pre_staged_path_passes_triage_pre_validation(tmp_path):
    """The auto-stage default for google_service_account
    (~/.config/gcloud/<project>-sa.json) MUST validate against the
    simple_actions allowlist — otherwise the cascade would refuse to
    touch its own pre-staged file later on."""
    classification = _classify_uploaded_json(SA_JSON_LITERAL)
    assert classification is not None
    proposed = Path(classification["suggested_target"]).expanduser()
    assert is_target_in_allowlist(str(proposed))


def test_triage_rejects_dangerous_target_even_with_known_kind():
    """Even if the LLM correctly picks a known kind, an unsafe target
    must be dropped — no /etc/passwd, no /root/."""
    da = {
        "kind": "write_file",
        "summary": "drop sa",
        "params": {"target": "/etc/passwd", "content": "x"},
    }
    assert _validate_direct_action(da) is None


def test_triage_accepts_safe_target(tmp_path):
    """The user's tmp_path is under /tmp which IS in the allowlist."""
    da = {
        "kind": "write_file",
        "summary": "drop sa",
        "params": {
            "target": str(tmp_path / "sa.json"),
            "content": "ok",
        },
    }
    out = _validate_direct_action(da)
    assert out is da
