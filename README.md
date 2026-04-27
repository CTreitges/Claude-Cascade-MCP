# Cascade-Bot-MCP

[![npm version](https://img.shields.io/npm/v/cascade-bot-mcp.svg)](https://www.npmjs.com/package/cascade-bot-mcp)
[![GitHub release](https://img.shields.io/github/v/release/CTreitges/cascade-bot-mcp)](https://github.com/CTreitges/cascade-bot-mcp/releases)
[![Tests](https://img.shields.io/badge/tests-457%20passing-brightgreen)](https://github.com/CTreitges/cascade-bot-mcp)

> A multi-agent **Plan → Implement → Review** loop you can talk to from
> Telegram, Claude Code, or the CLI. **v0.3.0 — the "self-healing
> cascade" release** ([changelog](CHANGELOG.md#030--2026-04-27)):
> broken quality-checks self-repair, integration review runs its own
> objective checks, and the planner sees full reviewer feedback on
> replan. Default implementer is `kimi-k2.6` — top of SWE-bench
> Verified April 2026 at 80.2 %.

Cascade-Bot watches a task all the way from "make me a thing" through
planning, implementation, quality-checks, and review — and keeps going
until every check passes (or the LLM-usage budget runs out). It's
designed to run as a **Claude Code MCP server**, with an optional
Telegram front-end for sending tasks from your phone.

```
You / Claude Code / Telegram
            │
            ▼
        Triage  (chat / direct-action / cascade)
            │
            ▼
   Plan → Implement → Review        ←── loops with stagnation guards
            │                            and 7-day rate-limit waits
            ▼
   ✅ done       ❌ failed (only on stagnation+budget exhausted)
```

---

## Why MCP, not a standalone product

The cascade orchestrator (`cascade.core.run_cascade`) is **the same code**
whether you reach it from Claude Code, Telegram, or the CLI. It's
shipped as an MCP server so you can call it from inside Claude Code with
`mcp__cascade__run_cascade_tool(task=...)` and have:

1. **Plan + Review on your local Claude CLI** — Opus + Sonnet calls go
   through the same `claude` binary you already have. As long as you're
   logged into Claude (e.g. via the Max plan), there are **no API costs
   for those steps**. The cascade isn't selling you LLM access — it just
   orchestrates calls you already have.

2. **Implementer on a separate cloud LLM** — code generation is the
   high-volume step, so it's offloaded to a cheap/fast model
   (Ollama Cloud's kimi-k2.6 by default — top of SWE-bench Verified
   April 2026 at 80.2%; qwen3-coder:480b / DeepSeek / GLM / MiniMax via
   Ollama or OpenAI-compatible endpoints; or Claude itself if you prefer).

3. **Long-running tasks survive your IDE** — Claude Code sessions are
   ephemeral; cascade tasks live in SQLite under `~/cascade-bot-mcp/store/`
   and a Telegram bot (optional) gives you status updates from anywhere.

You bring your own Claude Code subscription and your own LLM API keys.
Cascade is the loop logic + memory + UX glue that turns those into a
reliable agentic workflow.

---

## Architecture

```
Telegram / Claude Code (MCP) / CLI
              │
              ▼  Triage (3-mode: chat / direct-action / cascade)
   ┌──────────────────────────────────────┐
   │  Cascade Core (asyncio)              │
   │  Plan → Implement → Review           │
   │  + Quality-Checks (hard gate)        │
   │  + Stagnation-Detection (force-replan│
   │    on identical reviewer feedback)   │
   │  + HealingMonitor (stuck/perm-denied │
   │    diagnostic events)                │
   │  + with_retry (7-day budget — auto-  │
   │    waits Claude weekly-session caps) │
   └──────────┬───────────────────────────┘
              │
   ┌──────────┼─────────────────────────┐
   ▼          ▼            ▼            ▼
Planner   Implementer   Reviewer    Skill-
(Claude)  (Cloud LLM)   (Claude)    Suggester
DE/EN     +shortcut     strict      (Claude)
              │
              ▼
       SQLite (chat_messages + FTS5 +
       chat_summaries + pending_attachments
       + iterations + skills) + RLM (BM25)
```

| Worker          | Default Model       | Configurable via |
|-----------------|---------------------|------------------|
| Planner         | `claude-opus-4-7`   | `/models`, `.env` |
| Implementer     | `kimi-k2.6` (Ollama Cloud) | `/models` (4 cloud picks) |
| Reviewer        | `claude-sonnet-4-6` | `/models`, `.env` |
| Triage          | `claude-sonnet-4-6` | `.env` |
| Skill suggester | uses planner model  | (auto) |

The bot's chat-memory layer keeps:

- **Hot tier** — last 30 messages verbatim with inline file content
  (≤ 30 KB per upload) and a JSON classification (e.g.
  `google_service_account`).
- **Warm tier** — older messages → Sonnet-summarised in a background
  task every 6 h.
- **Long tier** — RLM (BM25 ranking + DE/EN stop-words + importance
  boost).

---

## Quick start — TL;DR

```bash
# 1. clone + venv
git clone https://github.com/CTreitges/cascade-bot-mcp.git ~/cascade-bot-mcp
cd ~/cascade-bot-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. interactive setup wizard (Telegram token, provider keys, …)
.venv/bin/cascade-setup

# 3. wire it into Claude Code (one-liner via npx — no Python paths needed)
claude mcp add cascade -- npx -y cascade-bot-mcp

# 4. (optional) start the Telegram bot
.venv/bin/python bot.py
#   → on Telegram, open your bot and send /start
#     the FIRST message claims you as owner; settings auto-locked
```

That's it. `mcp__cascade__*` tools are now available in any Claude Code
session, and the Telegram bot is a fully-featured chat partner with
guided `/setup`, voice transcription, file uploads, etc.

> The npx wrapper at <https://www.npmjs.com/package/cascade-bot-mcp>
> just locates and launches the Python MCP server. The Python install
> from step 1 is still required.

---

## Setup — detailed

### 1. Clone and install dependencies

```bash
git clone https://github.com/CTreitges/cascade-bot-mcp.git ~/cascade-bot-mcp
cd ~/cascade-bot-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# For development (pytest, ruff):
# .venv/bin/pip install -r requirements-dev.txt
```

Python 3.11+ required.

### 2. Run the setup wizard

```bash
.venv/bin/cascade-setup
```

Walks you through:

- **Telegram bot token** (from `@BotFather` → `/newbot`)
- **Implementer provider** (Ollama / OpenAI-compatible / local Claude CLI)
- **Provider-specific API key**
- **Optional**: OpenAI key (Whisper voice), Brave Search key (live web),
  GitHub PAT (private repo pushes)

All answers are written to `~/cascade-bot-mcp/secrets.env` (chmod 0600,
gitignored). Your `.env` is **never** touched by the wizard. You can
keep using your own `.env` if you prefer manual config — `secrets.env`
just overrides on top.

The wizard does **not** ask for your Telegram user ID. Once the bot is
running, the **first user who messages it becomes the owner** — the
bot writes `TELEGRAM_OWNER_ID` to `secrets.env` automatically and
locks future updates to that account. So just send `/start` from your
own account before anyone else does.

### 3. (Optional) Install RLM-Claude for cross-session memory

The bot works without it (recall falls back to a local JSONL), but
RLM-Claude gives BM25-ranked recall across runs.

```bash
# Linux / WSL native:
bash scripts/install-rlm-claude.sh

# Windows host (will guide you through WSL2):
pwsh -File scripts/install-rlm-claude.ps1
```

### 4. Register the MCP server with Claude Code

Three options — pick whichever you prefer.

**A) Recommended: via npx wrapper** — published at
[npmjs.com/package/cascade-bot-mcp](https://www.npmjs.com/package/cascade-bot-mcp).
No Python paths to remember:

```bash
claude mcp add cascade -- npx -y cascade-bot-mcp
```

The wrapper just locates the Python MCP server you installed in step 1
(via `$CASCADE_HOME` → `.venv/bin/python` → fallback to `cascade-mcp` on
PATH). The Python install is still required.

**B) Via the launcher script** (no Node, auto-resolves venv / pipx):

```bash
claude mcp add cascade --scope user -- \
  bash ~/cascade-bot-mcp/scripts/mcp-launcher.sh
```

**C) Direct, hard-coded paths** (most explicit, no resolution magic):

```bash
claude mcp add cascade --scope user -- \
  ~/cascade-bot-mcp/.venv/bin/python ~/cascade-bot-mcp/mcp_server.py
```

### 5. Run tests + smoke-test the CLI

```bash
.venv/bin/pytest -q
.venv/bin/cascade "Erstelle hello.py das 'hi' druckt"
```

### 6. Telegram bot as a systemd-user-service

```bash
mkdir -p ~/.config/systemd/user
cp systemd/cascade-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cascade-bot
sudo loginctl enable-linger $USER       # survive logout / reboot
journalctl --user -u cascade-bot -f
```

The default service file points at `%h/cascade-bot-mcp/`. If you
cloned to a different path, edit `WorkingDirectory` and
`EnvironmentFile` accordingly (or set `CASCADE_HOME=<your-path>` in
`secrets.env`).

---

## Windows users

Run everything inside **WSL2** (Ubuntu). The Python parts are tested on
Linux only — RLM-Claude in particular is Linux-native. The PowerShell
helper `scripts/install-rlm-claude.ps1` checks your WSL setup and
forwards the install into the right distro for you.

> Use **pwsh** (PowerShell 7+) rather than the legacy Windows
> PowerShell 5 — better UTF-8 handling and the script is tested against
> 7.x.

---

## The npm wrapper (maintainers)

The `npm/` directory holds a Node-based launcher published as
[`cascade-bot-mcp`](https://www.npmjs.com/package/cascade-bot-mcp). It
is purely a delegator — the real MCP server is still the Python module.
Users `npx`-it; you only touch this if you ship a new wrapper version.

### One-time setup of an npm publish token

Plain `npm login` doesn't survive 2FA-strict accounts on a publish.
Use a granular access token instead:

1. <https://www.npmjs.com/settings/~/tokens> → **Generate New Token**
   → **Granular Access Token**.
2. Settings:
   - **Packages and scopes:** `cascade-bot-mcp` (only).
   - **Permissions:** `Read and write`.
   - **Bypass two-factor authentication when publishing:** ✅ **enabled**.
   - Expiration: 30–90 days.
3. Drop the token into `~/.npmrc` (gitignored on a sane home setup):
   ```bash
   npm config set //registry.npmjs.org/:_authToken "npm_xxxxxxxxxx"
   chmod 600 ~/.npmrc
   ```

### Cutting a new wrapper release

```bash
cd npm
npm pack --dry-run                      # confirm contents (currently 4 files, ~6 KB)
npm version <patch|minor|major>         # bumps npm/package.json + creates a git tag
cd ..
git push && git push --tags             # publish the version-bump commit + tag
cd npm && npm publish --access public   # ships to registry.npmjs.org
```

`--access public` is required: without it npm tries a private publish
(which costs money and 403s on free accounts).

### Common publish errors

| Error | Cause | Fix |
|---|---|---|
| `403 ... bypass 2fa enabled is required` | Token created without "Bypass 2FA" flag, OR account 2FA is set to "Authorization and writes" | Re-create the token with the bypass checkbox; or fall back to `--otp=<6-digit-code>` per publish. |
| `402 Payment Required` | `--access public` missing | add the flag |
| `EPUBLISHCONFLICT <version> already exists` | npm forbids re-publishing the same version, even after `unpublish` | bump the version |
| `ENOENT: package.json` | running `npm publish` outside `npm/` | `cd npm` first |

---

## What's included

### MCP tools (exposed to Claude Code)

| Tool | Purpose |
|------|---------|
| `mcp__cascade__run_cascade_tool` | Run a Plan→Implement→Review cascade. `sync=True` blocks; `sync=False` returns a `task_id` for polling. |
| `mcp__cascade__cascade_status` | Status / iteration / summary of a task. |
| `mcp__cascade__cascade_logs`   | Raw last N log lines for a task (debug-level). |
| `mcp__cascade__cascade_progress` | **Telegram-identical milestone lines** for live polling. Cursor via `last_ts`; same formatter the bot uses. Used by `/cascade`. |
| `mcp__cascade__cascade_summary` | One-shot post-run bundle: status + plan + changed_files + recent reviews + diff_excerpt. |
| `mcp__cascade__cascade_cancel` | Cancel a running task (only those started in the same MCP process). |
| `mcp__cascade__cascade_history`| Recent N tasks across interfaces. |
| `mcp__cascade__cascade_resume` | Resume an interrupted task. |
| `mcp__cascade__cascade_dryrun` | Plan-only call (no implementer / reviewer) — cheap planning preview. |
| `mcp__cascade__cascade_skills_list` | List saved reusable skills. |
| `mcp__cascade__cascade_skill_run`  | Run a saved skill with `{placeholder}` arguments. |

There's also a `/cascade <task>` slash-command at
`~/.claude/commands/cascade.md` that wraps these tools — it dispatches
async, polls `cascade_progress` every 15s, prints the same milestone
lines you see in the Telegram bot, and finally calls `cascade_summary`
so the rest of the chat session has full context for follow-ups (diff
review, commit message, next steps).

Flags supported by `/cascade`:
`--repo /path`, `--implementer <model>`, `--planner <model>`,
`--reviewer <model>`, `--effort <low|medium|high|xhigh|max>`,
`--lang <de|en>`, `--sync` (skip polling, block until done).

### Telegram commands (highlights — `/help` for everything)

| Command | Purpose |
|---------|---------|
| `/start` | Welcome + setup-status check |
| `/setup` | Guided wizard for API keys |
| `/status [id]` / `/logs [id]` / `/diff [id]` | Task introspection |
| `/queue` / `/wait` / `/cancel` / `/abort` | Task control |
| `/again [id]` / `/resume <id>` | Retry / continue failed tasks |
| `/skills` / `/run <name>` / `/skillupgrade` | Reusable skill templates |
| `/repo <path>` / `/lang <de\|en>` / `/models` / `/effort` | Per-chat config |
| `/replan [n]` / `/iterations [n]` / `/failsbeforereplan [n]` / `/subtasks [n]` | Budget knobs |
| `/toggles` | Triage / Auto-Skill / Context7 / Web-Search / Auto-Decompose / Multi-Plan |
| `/forget` / `/chat` | Memory control |
| `/exec <cmd>` / `/git <repo> <subcmd>` | Shell + git (whitelisted) |

---

## Privacy / what stays local

The repo is **fully clean of personal data** — no real tokens, owner
IDs, or project IDs are committed. Specifically:

- `.env`, `secrets.env`, anything under `store/` (DB / logs / RLM
  fallback), and `workspaces/` are gitignored.
- `.env.example` ships empty placeholders only.
- The setup wizard writes API keys to `secrets.env` (chmod 0600) and
  never touches `.env`.
- Author metadata in `pyproject.toml` / `npm/package.json` / `LICENSE`
  is a generic "Cascade-Bot Contributors".

So you can `git push origin cascade-bot-mcp` straight from your local
clone without leaking anything.

---

## How the loop works

1. **Triage** — every incoming message is classified into chat / direct-
   action / full-cascade. Direct actions go through a one-pass quick
   reviewer; full cascades enter the main loop.
2. **Plan** — Opus turns the task into steps + acceptance criteria +
   quality-checks. If the task is small, the planner emits `direct_ops`
   instead and skips the loop entirely.
3. **Implement** — Ollama / OpenAI-compat / Claude generates a JSON
   list of file ops. `apply_ops` validates Python via `ast.parse`,
   JSON/TOML/YAML via parsers, and rejects function bodies that are
   bare `pass` / `...` / `raise NotImplementedError`.
4. **Quality checks** — every command in `plan.quality_checks` runs in
   the workspace. Plus auto-appended `python3 -m py_compile` and (when
   ruff is installed) `ruff check`.
5. **Review** — Sonnet judges the diff against the plan AND the
   original task. Pass requires every quality check ✅, every
   acceptance criterion mentioned, and no TODO/FIXME stubs.
6. **Replan / stagnation** — identical reviewer feedback two iterations
   in a row triggers an immediate replan. After `cascade_replan_max`
   replans (default 2) with continued stagnation, the run ends with
   `status='failed'` instead of looping forever.
7. **Wait + retry** — rate-limit / weekly-session-cap errors → the run
   waits up to 7 days for the next window. The `/wait` command shows
   ETAs.
8. **Lessons learned** — successful runs spawn a brief Sonnet self-
   critique that's persisted as an RLM finding so similar future tasks
   start with that hindsight as context.

---

## Configuration

Defaults live in `cascade/config.py` (`Settings` class). Overrides come
from, in order of precedence:

1. CLI / function arguments to `run_cascade(...)`
2. Per-chat `sessions` table (Telegram-only)
3. `<CASCADE_HOME>/secrets.env`  ← wizard writes here
4. `secrets.env` in repo root (alternative wizard target)
5. `.env` in repo root  ← user-edited, never touched by wizard
6. Built-in defaults

Notable knobs:

- `CASCADE_MAX_ITERATIONS` — default 999 (effectively unlimited; only
  LLM-usage budget stops a run)
- `CASCADE_REPLAN_MAX` — default 2; configurable per-chat via `/replan`
- `CASCADE_REPLAN_AFTER_FAILURES` — default 2; per-chat via
  `/failsbeforereplan`
- `CASCADE_DEBUG` — verbose rotating debug log
- `CASCADE_SUMMARIZE_ENABLED` — background chat summariser (default on)
- `CASCADE_AUTO_RESUME_INTERRUPTED` — resume orphan running tasks on
  bot startup (default off; the inline keyboard handles the per-task
  ask either way)

---

## Releasing a new version

CI + release are wired through `.github/workflows/`:

- `ci.yml` — runs on every push to `main`/`master`, every PR, and every
  `v*` tag. Lints with ruff and runs `pytest -q`.
- `release.yml` — triggers when a `v*` tag is pushed (or via "Run
  workflow" with a tag input from the Actions tab). Builds an sdist +
  wheel and creates a GitHub Release whose body is the matching
  `## [<version>]` section pulled out of `CHANGELOG.md`.

To cut a release:

1. Update `CHANGELOG.md` with a new `## [X.Y.Z] — YYYY-MM-DD` section.
2. Bump `version` in `pyproject.toml` and `npm/package.json`.
3. Commit and push to `main`.
4. Tag and push:
   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```
5. The `release.yml` workflow does the rest. If the tag was pushed
   before the workflow existed (e.g. `v0.2.0`), use **Actions → Release
   → Run workflow** with the tag name as input.

If the wrapper changed, ship a matching npm version too — see
[§ The npm wrapper](#the-npm-wrapper-maintainers) above. Wrapper and
Python releases version-bump independently; both currently sit at
0.2.0 but only need to stay aligned at major releases.

---

## License

MIT — see [LICENSE](LICENSE).
