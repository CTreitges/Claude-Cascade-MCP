# Claude-Cascade

Multi-Agent **Plan → Implement → Review** loop with hard-gated quality checks,
auto-replan, auto-skill-suggestion, and three interfaces:

- **MCP-Server** for Claude Code (`mcp__cascade__*`)
- **Telegram-Bot** with voice / vision / shell / git
- **CLI** (`cascade "<task>"`)

---

## Architecture

```
Telegram / Claude Code (MCP) / CLI
              │
              ▼
   ┌──────────────────────────────┐
   │  Cascade Core (asyncio)      │
   │  Plan → Implement → Review   │
   │  + Quality-Checks (hard gate)│
   │  + Replan-on-Failure         │
   └──────────┬───────────────────┘
              │
   ┌──────────┼─────────────────────────┐
   ▼          ▼            ▼            ▼
Planner   Implementer   Reviewer    Skill-
(Claude)  (Cloud LLM)   (Claude)    Suggester
                                    (Claude)
              │
              ▼
       SQLite + RLM
```

| Worker        | Default Model        | Configurable via |
|---------------|----------------------|------------------|
| Planner       | `claude-opus-4-7`    | `/models`, `.env` |
| Implementer   | `qwen3-coder:480b` (Ollama Cloud) | `/models` (4 cloud picks) |
| Reviewer      | `claude-sonnet-4-6`  | `/models`, `.env` |
| Triage        | `claude-sonnet-4-6`  | `.env` |
| Skill suggester | uses planner model | (auto) |

---

## Setup

```bash
git clone https://github.com/CTreitges/Claude-Cascade-MCP.git ~/claude-cascade
cd ~/claude-cascade
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

cp .env.example .env
# Required: TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID, OLLAMA_CLOUD_API_KEY
# Optional: OPENAI_API_KEY (for Whisper voice transcription)

.venv/bin/pytest -q          # 144 tests should be green
```

### CLI smoke-test

```bash
.venv/bin/cascade "Erstelle hello.py das 'hi' druckt"
```

### Register MCP server in Claude Code

```bash
claude mcp add cascade --scope user -- \
  /home/chris/claude-cascade/.venv/bin/python \
  /home/chris/claude-cascade/mcp_server.py
```

In a new Claude Code session:

```
mcp__cascade__run_cascade_tool(task="…", repo="/path", sync=true)
mcp__cascade__cascade_status(task_id="…")
mcp__cascade__cascade_logs(task_id="…", tail=50)
mcp__cascade__cascade_cancel(task_id="…")
mcp__cascade__cascade_history(limit=10)
```

There's also a `/cascade <task>` slash-command at `~/.claude/commands/cascade.md`.

### Telegram bot as systemd-user-service

```bash
mkdir -p ~/.config/systemd/user
cp systemd/cascade-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cascade-bot
sudo loginctl enable-linger $USER       # survive logout / reboot
journalctl --user -u cascade-bot -f
```

---

## How the loop works

```
1. Planner (Opus) reads the task + locally-discovered repos.
   Returns:  steps, files_to_touch, acceptance_criteria,
             repo: {kind: local|clone|fresh, path?, url?},
             quality_checks: [{name, command, must_succeed, ...}, ...]

2. Resolve workspace:
   --repo <path>           caller wins
   plan.repo.kind=local    Workspace.attach(path)
   plan.repo.kind=clone    git clone --depth 1 url → workspaces/<tid>-clone/
   plan.repo.kind=fresh    Workspace.create(tmp)

3. Loop (max 5 iterations by default):
     Implementer (Cloud LLM) gets plan + workspace files + relevant FILE
       CONTENTS (sandboxed read of plan.files_to_touch + basename matches).
       Returns FileOps[].
     workspace.apply_ops(ops)
     run quality_checks  → CheckResult[]
     Reviewer (Sonnet) sees plan + diff + check results.
       Hard gate: any failing check forces pass=false.
     If pass → done.
     If 2 consecutive fails AND replan budget left
       → Planner gets failure history, may rewrite plan + checks.

4. After done: skill_suggester (Opus) checks if the recent task pattern
   should become a reusable skill. User accepts via inline button.
```

---

## Telegram bot — commands

| Command | Effect |
|---|---|
| Text / voice / photo+caption | start a cascade run |
| `/status [id]` | latest or specified task status |
| `/logs [id]` | last 50 log lines |
| `/cancel [id]` | cancel running task |
| `/history` | last 10 tasks |
| `/resume <id>` | resume an interrupted task |
| `/repo <path>` | set default repo for this chat (`clear` to remove) |
| `/exec <cmd>` | run shell command (60s timeout, 4kB cap) |
| `/git <repo> <subcmd>` | whitelist: status / log / diff / branch / checkout / pull / push / commit / show |
| `/lang de\|en` | switch bot language (DE/EN) |
| `/models` | inline keyboard: pick worker → pick model |
| `/effort` | inline keyboard: pick worker → pick effort (low/medium/high/xhigh/max) |
| `/replan [n]` | replan budget (0..10), or no-arg for inline keyboard |
| `/skills` | list saved skills (auto-suggested after runs) |
| `/skills delete <name>` | remove a saved skill |
| `/run <skill_name> [args]` | run a saved skill — `{file}=foo.py` or positional `{0}` |
| `/help` | command overview |

**Smart triage** runs before every text message: a fast Sonnet call decides
whether the message is a *task* (→ start cascade) or *conversation* (→
short Sonnet reply with context of the last 3 tasks). Falls back to a regex
heuristic if Claude is unreachable.

**Auto-skill-suggestion** fires after every successful run: Opus checks
whether the recent task pattern is worth saving as a parametrised skill.
Suggestion is shown with `💾 Save / ❌ Discard` inline buttons. Cooldown
prevents spam. Set `CASCADE_AUTO_SKILL_SUGGEST=false` to disable.

**Auto-resume** at bot start: any leftover `running` task in SQLite is
marked `interrupted` and the owner is notified. Use `/resume <id>` to
continue.

---

## Implementer model catalog (curated /models picks)

Verified against `https://ollama.com/v1/models`:

- `glm-5.1`
- `kimi-k2.6`
- `minimax-m2.7`
- `deepseek-v4-flash`

`qwen3-coder:480b` stays as the runtime default (`CASCADE_IMPLEMENTER_MODEL`)
but isn't shown in the menu. OpenAI-compatible providers (GLM/DeepSeek/MiniMax/
Kimi via direct API) are also wired — see `cascade/llm_client.py` and
`Settings.openai_compat_credentials`.

---

## Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | from @BotFather |
| `TELEGRAM_OWNER_ID` | — | numeric Telegram user id (only this user is served) |
| `OLLAMA_CLOUD_API_KEY` | — | from ollama.com |
| `OPENAI_API_KEY` | — | optional, only for Whisper voice |
| `CASCADE_BOT_LANG` | `de` | `de` or `en` |
| `CASCADE_TIMEZONE` | `Europe/Berlin` | IANA TZ, used for `/logs` timestamps |
| `CASCADE_IMPLEMENTER_PROVIDER` | `ollama` | `ollama` or `openai_compatible` |
| `CASCADE_IMPLEMENTER_MODEL` | `qwen3-coder:480b` | tag |
| `CASCADE_IMPLEMENTER_TOOLS` | `fileops` | `fileops` or `mcp` |
| `CASCADE_PLANNER_MODEL` | `claude-opus-4-7` | |
| `CASCADE_REVIEWER_MODEL` | `claude-sonnet-4-6` | |
| `CASCADE_TRIAGE_MODEL` | `claude-sonnet-4-6` | |
| `CASCADE_PLANNER_EFFORT` | `` | empty = no `--effort` flag |
| `CASCADE_REVIEWER_EFFORT` | `` | |
| `CASCADE_TRIAGE_EFFORT` | `` | |
| `CASCADE_TRIAGE_ENABLED` | `true` | set false to dispatch every text as task |
| `CASCADE_MAX_ITERATIONS` | `5` | per run |
| `CASCADE_REPLAN_AFTER_FAILURES` | `2` | trigger replan after N consecutive fails |
| `CASCADE_REPLAN_MAX` | `2` | max planner re-invocations per run |
| `CASCADE_AUTO_SKILL_SUGGEST` | `true` | offer skills after successful runs |
| `CASCADE_SKILL_SUGGEST_COOLDOWN_S` | `300` | suggestion rate-limit |
| `CASCADE_WORKSPACE_RETENTION_DAYS` | `7` | tmp workspace cleanup window |

Per-chat overrides (set via Telegram, persisted in SQLite):
`/repo`, `/models`, `/effort`, `/replan`, `/lang`.

---

## Tests

```bash
.venv/bin/pytest -q     # 144 passing
.venv/bin/ruff check .  # clean
```

Test surface:

- `test_smoke` — package imports, default settings
- `test_workspace`, `test_workspace_read`, `test_workspace_attached_and_checks`
  — sandboxed FileOps, file-content reads, attached-mode no-pollution,
  quality-check execution
- `test_store`, `test_store_models` — SQLite schema, sessions, model overrides
- `test_claude_cli` — JSON envelope parsing
- `test_llm_client` — provider routing (Ollama vs OpenAI-compatible)
- `test_agents` — Plan / ReviewResult / ImplementerOutput pydantic schemas
- `test_core` — orchestrator end-to-end with mocked agents (cancel, resume,
  fail-after-max-iter, progress callbacks)
- `test_core_quality_loop` — quality-check hard gate + retry-to-pass
- `test_replan` — replan trigger after N failures, budget cap, no-replan-on-pass
- `test_repo_resolver` — local-repo discovery + clone fallback (uses real
  local `git clone` via file URL)
- `test_models_triage` — implementer catalog, triage cooldown / threshold /
  parse-error gating, claude-vs-heuristic fallback
- `test_effort_replan` — effort flag plumbing, store persistence
- `test_skills` — skill CRUD, suggester gating, template substitution

---

## Security notes

- Owner-check is the **first** middleware — unauthorized updates are silently
  dropped.
- `apply_ops` validates every path with `Path.resolve().is_relative_to(root)`
  to block `../` escapes and symlink hijacks.
- `/exec` caps timeout (60s) and output (4kB), uses `shlex.split`-style
  arg passing.
- `/git` enforces a subcommand whitelist.
- `.env` is in `.gitignore`; secrets never committed.
- Quality-check commands run with `cwd=workspace.root` and a 60s default
  timeout.
- Workspace attach mode never commits to the user's repo (`commit_iteration`
  is a no-op). Diffs use `base_ref` = HEAD-at-attach-time.

---

## Repo layout

```
~/claude-cascade/
├── pyproject.toml
├── README.md                    # this file
├── .env / .env.example          # config
├── cascade/
│   ├── core.py                  # Plan→Implement→Review orchestrator
│   ├── workspace.py             # Sandboxed FileOps + git-diff + run_check
│   ├── store.py                 # aiosqlite (tasks / iterations / logs / sessions / skills)
│   ├── claude_cli.py            # `claude -p` subprocess wrapper
│   ├── llm_client.py            # Ollama Cloud + OpenAI-compatible router
│   ├── memory.py                # RLM-Claude bridge (best-effort stub)
│   ├── repo_resolver.py         # discover local repos, resolve plan.repo
│   ├── skill_suggester.py       # post-run "is this worth a skill?"
│   ├── triage.py                # task-vs-chat classifier
│   ├── i18n.py                  # bot i18n (DE/EN)
│   ├── models.py                # implementer/planner-reviewer model catalog
│   ├── config.py                # pydantic-settings
│   └── agents/{planner,implementer,reviewer}.py
├── mcp_server.py                # FastMCP stdio server
├── bot.py                       # python-telegram-bot v21+
├── systemd/cascade-bot.service  # user-service template
├── store/                       # SQLite db lives here
├── workspaces/                  # tmp dirs per task (auto-cleanup)
└── tests/
```
