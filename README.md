# Claude-Cascade

Multi-Agent **Plan → Implement → Review** Loop, erreichbar als **MCP-Server** (für Claude Code) und als **Telegram-Bot**.

```
Telegram / Claude Code (MCP)
            │
            ▼
   ┌────────────────────┐
   │  Cascade Core      │  asyncio orchestrator, max N iterations
   └────────┬───────────┘
            │
   ┌────────┼─────────┐
   ▼        ▼         ▼
Planner  Implementer  Reviewer
(Opus)   (Cloud LLM)  (Sonnet)
            │
            ▼
       SQLite + RLM
```

- **Planner**: `claude -p --model claude-opus-4-7` — strukturierter Plan
- **Implementer**: konfigurierbares Cloud-LLM (Ollama Cloud `qwen3-coder:480b`, GLM, DeepSeek, MiniMax, Kimi …) — gibt JSON-FileOps zurück
- **Reviewer**: `claude -p --model claude-sonnet-4-6` — pass/fail + Feedback
- **Loop**: max 3 Iterationen, dann Done oder Failed
- **Persistenz**: SQLite (`store/cascade.db`) + best-effort RLM-Bridge
- **Auto-Resume**: Bot markiert beim Start verwaiste `running`-Tasks als `interrupted` und benachrichtigt den Owner

---

## Setup

```bash
git clone … ~/claude-cascade
cd ~/claude-cascade
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

cp .env.example .env
# Pflicht: TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID
# Implementer-Backend: OLLAMA_CLOUD_API_KEY oder einer der OpenAI-kompatiblen Keys
# Voice: OPENAI_API_KEY (Whisper)

.venv/bin/pytest -q   # 61 Tests grün erwartet
```

## CLI-Smoke-Test

```bash
.venv/bin/cascade "Erstelle hello.py das 'hi' druckt"
```

## MCP in Claude Code registrieren

```bash
claude mcp add cascade -- /home/chris/claude-cascade/.venv/bin/python /home/chris/claude-cascade/mcp_server.py
```

In einer Claude-Code-Session dann:

```
mcp__cascade__run_cascade_tool(task="...")
mcp__cascade__cascade_status(task_id="...")
mcp__cascade__cascade_logs(task_id="...", tail=50)
mcp__cascade__cascade_cancel(task_id="...")
mcp__cascade__cascade_history(limit=10)
```

## Telegram-Bot als systemd-User-Service

```bash
mkdir -p ~/.config/systemd/user
cp systemd/cascade-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cascade-bot
systemctl --user status cascade-bot
journalctl --user -u cascade-bot -f
```

Der Service braucht eine User-Linger-Aktivierung damit er Reboots überlebt:

```bash
sudo loginctl enable-linger $USER
```

## Bot-Commands

| Command | Zweck |
|---------|-------|
| Text/Voice/Photo+Caption | Cascade-Run starten |
| `/status [id]` | Letzten / spezifischen Task |
| `/logs [id]` | Letzte 50 Log-Zeilen |
| `/cancel [id]` | Cancel laufender Task (oder Chat-Inflight) |
| `/history` | Letzte 10 Tasks |
| `/resume <id>` | Interrupted Task fortsetzen |
| `/repo <path>` | Default-Repo für Folge-Tasks setzen (`clear` zum Leeren) |
| `/exec <cmd>` | Subprocess (60s Timeout, 4kB-Cap) |
| `/git <repo> <subcmd>` | Whitelist: status, log, diff, branch, checkout, pull, push, commit, show |
| `/help` | Übersicht |

Sicherheit: alle Updates ohne `effective_user.id == TELEGRAM_OWNER_ID` werden silent ignoriert (Logging „ignored unauthorized user X").

## Implementer-Modelle

`CASCADE_IMPLEMENTER_PROVIDER=ollama` und `CASCADE_IMPLEMENTER_MODEL=qwen3-coder:480b` ist der Default.

OpenAI-kompatible Provider via Modellpräfix:
- `glm-…` → `GLM_API_KEY` / `GLM_BASE_URL`
- `deepseek-…` → `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL`
- `minimax-…` / `abab-…` → `MINIMAX_API_KEY` / `MINIMAX_BASE_URL`
- `kimi-…` / `moonshot-…` → `KIMI_API_KEY` / `KIMI_BASE_URL`

Pro Run überschreibbar via `run_cascade_tool(implementer_model=…, implementer_provider="openai_compatible")`.

## Tests

```bash
.venv/bin/pytest -q
```

Aktuell:

- `test_smoke.py` — Imports + Default-Settings
- `test_workspace.py` — Pfad-Sandboxing, FileOps, Git-Diff (18 Tests)
- `test_store.py` — SQLite Schema + Helpers (10 Tests)
- `test_claude_cli.py` — JSON-Parsing der Claude-Antworten (7 Tests)
- `test_llm_client.py` — Provider-Routing, Mock-Calls (7 Tests)
- `test_agents.py` — Pydantic-Schemas + Prompt-Aufbau (12 Tests)
- `test_core.py` — End-to-End-Loop mit gemockten Agents (7 Tests)

## Sicherheits-Notizen

- Owner-Check ist erste Filter-Middleware
- `apply_ops` validiert `Path.resolve().is_relative_to(workspace_root)` — verhindert `../` / Symlink-Escape
- `/exec` cappt Timeout 60s und Output 4kB
- `/git` mit Subcommand-Whitelist
- `.env` gehört nicht ins Repo (`.gitignore` enthält bereits den Pattern)
