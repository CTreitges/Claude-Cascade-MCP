"""Interactive CLI setup wizard.

Run BEFORE starting the bot for the first time:

    cascade-setup

The wizard walks the user through:
  1. Telegram bot token  (from @BotFather)
  2. Implementer provider + API key
  3. Optional helpers (Whisper, Brave, GitHub PAT)

Owner ID is NOT asked — the bot auto-detects it from the FIRST
incoming Telegram message after start (see `cascade.bot.helpers`).

All answers are written to `<CASCADE_HOME>/secrets.env` (chmod 0600,
gitignored). The user's `.env` is never touched.

The same wizard is also reachable as `/setup` inside the bot when it's
running — same logic, different surface (Telegram instead of terminal).
"""

from __future__ import annotations

import re
import sys
from getpass import getpass

from .secrets_store import secrets_path, set_secret


_VALID_TG_TOKEN = re.compile(r"^\d{6,12}:[A-Za-z0-9_\-]{25,}$")
_VALID_API_KEY = re.compile(r"^[A-Za-z0-9._\-]{8,}$")


def _bold(text: str) -> str:
    return f"\033[1m{text}\033[0m" if sys.stdout.isatty() else text


def _yellow(text: str) -> str:
    return f"\033[33m{text}\033[0m" if sys.stdout.isatty() else text


def _green(text: str) -> str:
    return f"\033[32m{text}\033[0m" if sys.stdout.isatty() else text


def _ask(prompt: str, *, secret: bool = False, default: str = "") -> str:
    """Prompt the user. `secret=True` masks the input. Empty answer
    returns `default`."""
    full = f"  {prompt}"
    if default:
        full += f" [{default}]"
    full += ": "
    try:
        ans = (getpass(full) if secret else input(full)).strip()
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(130)
    return ans or default


def _confirm(prompt: str, *, default: bool = True) -> bool:
    sfx = " [Y/n]" if default else " [y/N]"
    a = _ask(prompt + sfx).lower()
    if not a:
        return default
    return a in ("y", "yes", "j", "ja")


def _section(title: str) -> None:
    print()
    print(_bold(f"== {title} =="))


def _mask(value: str) -> str:
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def main() -> int:
    print(_bold("\n🛠 Cascade-Bot Setup Wizard\n"))
    print(
        "I'll ask for the values you need to run the bot.\n"
        f"Answers are written to: {secrets_path()}\n"
        "(chmod 0600, gitignored — your .env stays untouched.)\n"
        "\nLeave any answer blank to skip that step.\n"
    )

    written: list[tuple[str, str]] = []

    # ---- 1. Telegram bot token ------------------------------------------
    _section("1/5  Telegram bot token")
    print("  Get one from @BotFather  (https://t.me/BotFather → /newbot)")
    print(f"  {_yellow('You can leave this blank only if you already set TELEGRAM_BOT_TOKEN in .env.')}")
    token = _ask("Bot token", secret=True)
    if token:
        if not _VALID_TG_TOKEN.match(token):
            print(_yellow("  ⚠️  Token looks unusual; saving anyway."))
        set_secret("TELEGRAM_BOT_TOKEN", token)
        written.append(("TELEGRAM_BOT_TOKEN", _mask(token)))

    # owner_id is NOT asked — auto-detected from the first /start message.
    print(
        f"  {_green('ℹ  owner ID will be auto-detected from your first message')}\n"
        "     after you start the bot. Just send /start to your bot from\n"
        "     your own Telegram account ONCE — the bot locks to that id."
    )

    # ---- 2. Implementer provider ---------------------------------------
    _section("2/5  Implementer provider")
    print(
        "  Which cloud LLM should generate code?\n"
        "    a) ollama              — Ollama Cloud (qwen3-coder, GLM, …)\n"
        "    b) openai_compatible   — DeepSeek / GLM / MiniMax / Kimi\n"
        "    c) claude              — local 'claude' CLI (Max-Subscription)\n"
    )
    pick = _ask("Pick a/b/c  (or skip)", default="a").lower().strip()
    provider = {
        "a": "ollama", "ollama": "ollama",
        "b": "openai_compatible", "openai_compatible": "openai_compatible",
        "c": "claude", "claude": "claude",
    }.get(pick, "")
    if provider:
        set_secret("CASCADE_IMPLEMENTER_PROVIDER", provider)
        written.append(("CASCADE_IMPLEMENTER_PROVIDER", provider))

    if provider == "ollama":
        print("\n  Ollama Cloud API key — free at https://ollama.com/account")
        ans = _ask("API key", secret=True)
        if ans and _VALID_API_KEY.match(ans):
            set_secret("OLLAMA_CLOUD_API_KEY", ans)
            written.append(("OLLAMA_CLOUD_API_KEY", _mask(ans)))

    elif provider == "openai_compatible":
        print(
            "\n  Which provider?  glm / deepseek / minimax / kimi "
            f"{_yellow('(default: glm)')}",
        )
        flav = _ask("Provider", default="glm").lower().strip()
        if flav not in ("glm", "deepseek", "minimax", "kimi"):
            print(_yellow(f"  ⚠️  unknown flavour '{flav}', skipping"))
        else:
            print(f"\n  {flav.upper()} API key:")
            ans = _ask("API key", secret=True)
            if ans and _VALID_API_KEY.match(ans):
                key_var = f"{flav.upper()}_API_KEY"
                set_secret(key_var, ans)
                written.append((key_var, _mask(ans)))
            suggested = {
                "glm": "glm-5.1",
                "deepseek": "deepseek-v4",
                "minimax": "minimax-m2.7",
                "kimi": "kimi-k2.6",
            }[flav]
            set_secret("CASCADE_IMPLEMENTER_MODEL", suggested)
            written.append(("CASCADE_IMPLEMENTER_MODEL", suggested))

    elif provider == "claude":
        print(
            "\n  No API key needed — auth piggy-backs on your local 'claude' CLI.\n"
            "  Verify it works:  claude --help\n"
            "  Not installed?    https://docs.claude.com/claude-code"
        )

    # ---- 3. Optional: Whisper voice -----------------------------------
    _section("3/5  OpenAI key (Whisper voice transcription)")
    print(f"  {_yellow('Optional')} — only needed for voice messages.")
    ans = _ask("OPENAI_API_KEY", secret=True)
    if ans and _VALID_API_KEY.match(ans):
        set_secret("OPENAI_API_KEY", ans)
        written.append(("OPENAI_API_KEY", _mask(ans)))

    # ---- 4. Optional: Brave Search -------------------------------------
    _section("4/5  Brave Search (web research)")
    print(f"  {_yellow('Optional')} — free key at https://api-dashboard.search.brave.com/")
    ans = _ask("BRAVE_SEARCH_API_KEY", secret=True)
    if ans and _VALID_API_KEY.match(ans):
        set_secret("BRAVE_SEARCH_API_KEY", ans)
        written.append(("BRAVE_SEARCH_API_KEY", _mask(ans)))

    # ---- 5. Optional: GitHub PAT --------------------------------------
    _section("5/5  GitHub Personal Access Token")
    print(
        f"  {_yellow('Optional')} — only for /git push against private repos.\n"
        "  Create at https://github.com/settings/tokens (scope: repo)"
    )
    ans = _ask("GITHUB_TOKEN", secret=True)
    if ans and _VALID_API_KEY.match(ans):
        set_secret("GITHUB_TOKEN", ans)
        written.append(("GITHUB_TOKEN", _mask(ans)))

    # ---- Summary --------------------------------------------------------
    print()
    print(_bold("== Summary =="))
    if not written:
        print(_yellow("  (no values saved)"))
    else:
        for k, v in written:
            print(f"  {_green('✓')} {k} = {v}")
    print()
    print(f"Stored in:  {secrets_path()}")
    print()
    print(_bold("Next steps:"))
    print("  1.  Start the bot:  ./bot.py   (or: systemctl --user start cascade-bot)")
    print("  2.  Open Telegram, find your bot, send /start.")
    print("     → owner-id auto-detect locks to your account.")
    print("  3.  Send /help inside the bot to see every command.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
