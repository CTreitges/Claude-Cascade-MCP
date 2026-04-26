"""Guided /setup wizard.

Walks the user through one provider at a time and writes the answers
into `secrets.env` (gitignored, owner-readable only). The user's
hand-edited `.env` is NEVER touched.

The wizard is conversational: each step asks a focused question via
`feedback.ask_user` and validates the answer where possible. The flow
adapts to the user's choices — e.g. picking Ollama as implementer
provider triggers an OLLAMA_CLOUD_API_KEY question; picking GLM
triggers GLM_API_KEY; etc.

Top-level flow:
  1. Confirm Telegram owner ID (must already be set; bot wouldn't have
     accepted /setup otherwise).
  2. Pick implementer provider (ollama / openai_compatible / claude).
  3. Provider-specific key(s).
  4. Optional: Whisper (OPENAI_API_KEY), Brave Search, GitHub PAT.
  5. Optional: RLM-Claude install pointer (Linux native vs WSL note).
  6. Restart hint so pydantic-settings picks the new file up.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode

from cascade.config import settings
from cascade.feedback import ask_user
from cascade.secrets_store import set_secret, secrets_path
from cascade.store import Store

from ..helpers import lang_for, owner_only

log = logging.getLogger("cascade.bot.handlers.setup")


_VALID_API_KEY = re.compile(r"^[A-Za-z0-9._\-]{8,}$")


async def _ask(store: Store, chat_id: int, label: str, *, timeout_s: int = 600) -> str:
    """Wrapper around ask_user that surfaces a clear `(skip)` fallback so
    the wizard never hangs forever on a forgotten step."""
    raw = await ask_user(store, chat_id, label, timeout_s=timeout_s, fallback="(skip)")
    return (raw or "").strip()


def _is_skipped(answer: str) -> bool:
    a = answer.lower()
    return a in ("", "(skip)", "skip", "überspringen", "überspring", "next", "nein", "no")


async def _step_provider(msg, lang: str) -> str | None:
    """Returns "ollama" / "openai_compatible" / "claude" — or None if user
    aborted."""
    head = (
        "*1️⃣ Implementer-Provider*\n\n"
        "Welcher Cloud-LLM soll den Code generieren?\n"
        "  • `ollama`              — Ollama Cloud (qwen3-coder, GLM, …)\n"
        "  • `openai_compatible`   — OpenAI-API kompatible Provider\n"
        "                            (DeepSeek / GLM / MiniMax / Kimi)\n"
        "  • `claude`              — über lokale `claude` CLI "
        "(Max-Subscription)\n\n"
        "Antwort: `ollama` / `openai_compatible` / `claude` "
        "(oder `skip` zum Überspringen)"
    ) if lang == "de" else (
        "*1️⃣ Implementer provider*\n\n"
        "Which cloud LLM should generate code?\n"
        "  • `ollama`              — Ollama Cloud (qwen3-coder, GLM, …)\n"
        "  • `openai_compatible`   — OpenAI-API compatible providers\n"
        "                            (DeepSeek / GLM / MiniMax / Kimi)\n"
        "  • `claude`              — via local `claude` CLI "
        "(Max-Subscription)\n\n"
        "Reply: `ollama` / `openai_compatible` / `claude` "
        "(or `skip` to leave default)"
    )
    await msg.reply_text(head, parse_mode=ParseMode.MARKDOWN)
    return None  # answered by ask_user in the caller


async def cmd_setup(update: Update, ctx) -> None:
    """Run the guided setup wizard. Idempotent — re-running just lets the
    user adjust values."""
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    msg = update.effective_message
    chat_id = update.effective_chat.id
    store: Store = ctx.application.bot_data["store"]
    s = settings()

    intro = (
        "🛠 *Geführtes Setup*\n\n"
        "Ich frage dich der Reihe nach nach den Konfigurations-Werten.\n"
        "Antworte direkt im Chat mit dem Wert, oder mit `skip` zum "
        "Überspringen.\n\n"
        f"Werte werden lokal gespeichert in:\n  `{secrets_path()}`\n"
        "_(0600 chmod, gitignored — niemals im Git-Push.)_\n\n"
        "Deine bestehende `.env` wird *nicht* angefasst."
    ) if lang == "de" else (
        "🛠 *Guided setup*\n\n"
        "I'll ask one question at a time. Reply with the value, or "
        "type `skip` to leave a value alone.\n\n"
        f"Values are stored locally at:\n  `{secrets_path()}`\n"
        "_(chmod 0600, gitignored — never pushed.)_\n\n"
        "Your existing `.env` will NOT be touched."
    )
    await msg.reply_text(intro, parse_mode=ParseMode.MARKDOWN)

    written: list[tuple[str, str]] = []  # (key, masked-display)

    # --- Step 1: implementer provider ---
    await _step_provider(msg, lang)
    provider_raw = await _ask(store, chat_id, "implementer-provider")
    provider = provider_raw.lower().strip()
    if _is_skipped(provider):
        provider = ""
    if provider in ("ollama", "openai_compatible", "claude"):
        set_secret("CASCADE_IMPLEMENTER_PROVIDER", provider)
        written.append(("CASCADE_IMPLEMENTER_PROVIDER", provider))

    # --- Step 2: provider-specific keys ---
    if provider == "ollama" or (not provider and not s.ollama_cloud_api_key):
        await msg.reply_text(
            "*2️⃣ Ollama Cloud API-Key*\n\n"
            "Hol dir einen kostenlosen Key auf https://ollama.com/account.\n"
            "Antwort: dein Key (oder `skip`)"
            if lang == "de" else
            "*2️⃣ Ollama Cloud API key*\n\n"
            "Free key: https://ollama.com/account\n"
            "Reply: your key (or `skip`)",
            parse_mode=ParseMode.MARKDOWN,
        )
        ans = await _ask(store, chat_id, "ollama-api-key")
        if not _is_skipped(ans) and _VALID_API_KEY.match(ans):
            set_secret("OLLAMA_CLOUD_API_KEY", ans)
            written.append(("OLLAMA_CLOUD_API_KEY", _mask(ans)))

    if provider == "openai_compatible":
        await msg.reply_text(
            "*2️⃣ OpenAI-kompatible Provider*\n\n"
            "Welcher Provider? `glm` / `deepseek` / `minimax` / `kimi`\n"
            "(oder `skip`)" if lang == "de" else
            "*2️⃣ OpenAI-compatible provider*\n\n"
            "Which one? `glm` / `deepseek` / `minimax` / `kimi`\n"
            "(or `skip`)",
            parse_mode=ParseMode.MARKDOWN,
        )
        flav = (await _ask(store, chat_id, "openai-compat-flavour")).lower().strip()
        if flav in ("glm", "deepseek", "minimax", "kimi"):
            await msg.reply_text(
                f"*{flav.upper()}-API-Key*\n\nAntwort: dein Key (oder `skip`)"
                if lang == "de" else
                f"*{flav.upper()} API key*\n\nReply: your key (or `skip`)",
                parse_mode=ParseMode.MARKDOWN,
            )
            ans = await _ask(store, chat_id, f"{flav}-api-key")
            if not _is_skipped(ans) and _VALID_API_KEY.match(ans):
                key_var = f"{flav.upper()}_API_KEY"
                set_secret(key_var, ans)
                written.append((key_var, _mask(ans)))
            # Suggest a sensible implementer model for that provider
            suggested = {
                "glm": "glm-5.1",
                "deepseek": "deepseek-v4",
                "minimax": "minimax-m2.7",
                "kimi": "kimi-k2.6",
            }.get(flav)
            if suggested:
                set_secret("CASCADE_IMPLEMENTER_MODEL", suggested)
                written.append(("CASCADE_IMPLEMENTER_MODEL", suggested))

    if provider == "claude":
        await msg.reply_text(
            "*2️⃣ Claude CLI*\n\n"
            "Stelle sicher dass `claude --help` auf der Konsole funktioniert.\n"
            "Kein API-Key nötig — Auth läuft über die Max-Subscription des "
            "lokalen `claude`-CLI.\n\n"
            "Wenn nicht installiert: https://docs.claude.com/claude-code"
            if lang == "de" else
            "*2️⃣ Claude CLI*\n\n"
            "Make sure `claude --help` works on this machine. No API key "
            "needed — auth piggy-backs on the local `claude` CLI's "
            "Max-Subscription.\n\n"
            "Not installed? https://docs.claude.com/claude-code",
            parse_mode=ParseMode.MARKDOWN,
        )

    # --- Step 3: Voice / OpenAI ---
    await msg.reply_text(
        "*3️⃣ OpenAI API-Key (für Whisper-Voice)*\n\n"
        "Optional. Nur nötig wenn du Voice-Memos schickst.\n"
        "Antwort: dein OpenAI-Key (oder `skip`)"
        if lang == "de" else
        "*3️⃣ OpenAI API key (for Whisper voice)*\n\n"
        "Optional. Only needed if you send voice messages.\n"
        "Reply: your OpenAI key (or `skip`)",
        parse_mode=ParseMode.MARKDOWN,
    )
    ans = await _ask(store, chat_id, "openai-api-key")
    if not _is_skipped(ans) and _VALID_API_KEY.match(ans):
        set_secret("OPENAI_API_KEY", ans)
        written.append(("OPENAI_API_KEY", _mask(ans)))

    # --- Step 4: Brave Search ---
    await msg.reply_text(
        "*4️⃣ Brave Search API-Key*\n\n"
        "Optional. Nur für Web-Suche im External-Context.\n"
        "Kostenloser Key: https://api-dashboard.search.brave.com/\n"
        "Antwort: dein Key (oder `skip`)"
        if lang == "de" else
        "*4️⃣ Brave Search API key*\n\n"
        "Optional. Powers web search in external context.\n"
        "Free key: https://api-dashboard.search.brave.com/\n"
        "Reply: your key (or `skip`)",
        parse_mode=ParseMode.MARKDOWN,
    )
    ans = await _ask(store, chat_id, "brave-api-key")
    if not _is_skipped(ans) and _VALID_API_KEY.match(ans):
        set_secret("BRAVE_SEARCH_API_KEY", ans)
        written.append(("BRAVE_SEARCH_API_KEY", _mask(ans)))

    # --- Step 5: GitHub PAT ---
    await msg.reply_text(
        "*5️⃣ GitHub Personal Access Token*\n\n"
        "Optional. Wird benötigt für `/git push`-Pfade gegen private Repos.\n"
        "Erstelle unter https://github.com/settings/tokens (scope: `repo`).\n"
        "Antwort: dein PAT (oder `skip`)"
        if lang == "de" else
        "*5️⃣ GitHub Personal Access Token*\n\n"
        "Optional. Used for `/git push` against private repos.\n"
        "Create at https://github.com/settings/tokens (scope: `repo`).\n"
        "Reply: your PAT (or `skip`)",
        parse_mode=ParseMode.MARKDOWN,
    )
    ans = await _ask(store, chat_id, "github-pat")
    if not _is_skipped(ans) and _VALID_API_KEY.match(ans):
        set_secret("GITHUB_TOKEN", ans)
        written.append(("GITHUB_TOKEN", _mask(ans)))

    # --- Step 6: RLM-Claude install hint ---
    await msg.reply_text(
        "*6️⃣ RLM-Claude (Long-Term-Memory)*\n\n"
        "Die Cascade nutzt RLM-Claude für persistente Findings. "
        "Installation:\n"
        "• *Linux/WSL:* `pip install rlm-claude` und MCP registrieren\n"
        "• *Windows:* nur via WSL2 — siehe README → Windows-Setup\n\n"
        "Soll ich den Linux-Installer-Befehl in die Zwischenablage "
        "echoen? (`ja`/`nein`)"
        if lang == "de" else
        "*6️⃣ RLM-Claude (long-term memory)*\n\n"
        "Cascade uses RLM-Claude for persistent findings. Install:\n"
        "• *Linux/WSL:* `pip install rlm-claude` + register MCP\n"
        "• *Windows:* via WSL2 only — see README → Windows setup\n\n"
        "Should I echo the install command? (`yes`/`no`)",
        parse_mode=ParseMode.MARKDOWN,
    )
    ans = (await _ask(store, chat_id, "rlm-install")).lower().strip()
    if ans in ("ja", "j", "yes", "y"):
        await msg.reply_text(
            "```\npip install rlm-claude\nrlm-claude init\nclaude mcp add "
            "rlm-claude --scope user -- rlm-claude serve\n```",
            parse_mode=ParseMode.MARKDOWN,
        )

    # --- Final summary ---
    if not written:
        await msg.reply_text(
            "✅ Setup abgeschlossen — keine Werte geändert."
            if lang == "de" else
            "✅ Setup done — no values changed.",
        )
        return

    summary_lines = [
        ("✅ *Setup abgeschlossen.* Geschrieben nach"
         if lang == "de" else "✅ *Setup complete.* Written to"),
        f"  `{secrets_path()}`",
        "",
        "*Aktualisierte Schlüssel:*" if lang == "de" else "*Keys updated:*",
    ]
    for k, v in written:
        summary_lines.append(f"  • `{k}` = `{v}`")
    summary_lines.append("")
    summary_lines.append(
        "🔄 *Bot neu starten* damit Pydantic die Werte lädt:\n"
        "`systemctl --user restart cascade-bot`"
        if lang == "de" else
        "🔄 *Restart the bot* so pydantic-settings picks the values up:\n"
        "`systemctl --user restart cascade-bot`"
    )
    await msg.reply_text(
        "\n".join(summary_lines), parse_mode=ParseMode.MARKDOWN,
    )


def _mask(value: str) -> str:
    """Show first 4 + last 4 chars only, replace the middle with stars.
    Keeps audit-friendly summaries without leaking the secret."""
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def is_setup_required(s=None) -> bool:
    """Heuristic: do we look like a fresh install? Used by /start to
    decide whether to nudge the user to /setup."""
    s = s or settings()
    # If neither Ollama nor any OpenAI-compat key is configured, the
    # implementer can't run — that's the canonical "not set up" signal.
    has_impl_key = bool(
        (s.cascade_implementer_provider == "ollama" and s.ollama_cloud_api_key)
        or (s.cascade_implementer_provider == "openai_compatible" and any([
            s.glm_api_key, s.deepseek_api_key, s.minimax_api_key, s.kimi_api_key,
        ]))
        or s.cascade_implementer_provider == "claude"
    )
    return not has_impl_key


# ---- Helpers exposed for tests ----


def _resolve_secrets_path() -> Path:
    return secrets_path()
