# Changelog

All notable changes are listed here. Versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-04-26

This is the "qualitative bot" release — the cascade now persists
context across messages, retries through Claude's weekly-session caps,
detects its own stagnation, validates every file before writing, and
ships a guided setup wizard. Headline change: out of the box the bot
no longer iterates a fixed number of times — it keeps going until
either every quality check passes or the LLM-usage budget is spent.

### Added

#### Conversational layer
- **`ChatMemory` (cascade/chat_memory.py)** — three-tier memory: Hot
  (last 30 turns verbatim, with file content inlined up to 30 KB),
  Warm (Sonnet-condensed summaries of older windows), Long (RLM with
  BM25 ranking). `build_context()` produces a single structured block
  for the triage prompt: USER FACTS · RECENT UPLOADS · CONVERSATION
  with `[FILE: …]` markers · EARLIER · SEARCH HITS.
- **`pending_attachments` table** — files received in the last 24 h are
  separately tracked so the triage can answer "did I receive a JSON
  earlier?" even when the message scrolled out of the Hot tier.
- **FTS5 search** — chat_messages mirror via SQLite FTS5; falls back to
  LIKE when FTS5 isn't compiled in.
- **`/forget`** — wipes chat_messages, chat_summaries, and
  pending_attachments in one shot.

#### Triage / dispatch
- **3-mode triage**: chat / direct-action / cascade.
- **Path pre-validation** — proposed `direct_action` targets are checked
  against `simple_actions._ALLOWED_ROOTS` before being returned.
- **File-awareness** in the system prompt — explicit "never reply 'I
  haven't received a file' when the RECENT UPLOADS block exists".
- **Tight retry budget** for triage (180s total, 10s min backoff) so
  the user-facing hot path doesn't block for hours on a quirk while
  longer-running cascade-internal calls keep their 7-day budget.

#### Self-healing
- **Iteration cap removed** — `cascade_max_iterations` defaults to 999
  (effectively unlimited). Stagnation-detection prevents infinite
  loops; replan budget remains capped (`cascade_replan_max`,
  configurable via `/replan`).
- **Stagnation detector** — when reviewer feedback is identical two
  iterations in a row, replan fires immediately (skipping
  `cascade_replan_after_failures`). When stagnation persists *and*
  replan budget is exhausted, the run ends with `failed` instead of
  looping until 999.
- **`with_retry` 7-day default budget** — cascades survive Claude's
  weekly-usage cap by waiting for the next session window.
  `parse_retry_after` now understands "Resets in N days".
- **`waiting_for_session` progress event** — surfaced via Telegram so
  the bot visibly says "⏳ waiting for session — ~3T 14h" instead of
  going silent during long retry waits.
- **`HealingMonitor` (cascade/healing.py)** — observes a running cascade
  and emits diagnostics for: stuck phases, permission-denied messages
  in logs/reviewer feedback, three identical implementer outputs in a
  row.
- **Pre-apply validation** — `workspace.apply_ops` now AST-parses .py,
  json.loads .json, tomllib.loads .toml, yaml.safe_load .yaml/.yml
  before writing. Function bodies that are bare `pass`, `...`, or
  `raise NotImplementedError` are rejected as stubs.
- **Auto-lint quality checks** — when a plan touches `.py` files, the
  supervisor appends `python3 -m py_compile` and (if available) `ruff
  check` to the quality checks.

#### UX & onboarding
- **`/setup`** — guided wizard that asks for implementer provider,
  API keys, optional Whisper / Brave / GitHub PAT, RLM install
  hint. Writes everything to `secrets.env` (chmod 0600, gitignored).
  Your hand-edited `.env` is never overwritten.
- **`/start`** — welcome message, architecture overview, and a
  prominent nudge to `/setup` when no implementer key is configured.
- **`/wait`** — shows which running tasks are currently sleeping on a
  rate-limit / weekly-session cap, with the estimated remaining time.
- **`/skillupgrade`** — Opus walks every saved skill, asks for any
  necessary clarifications via `ask_user`, and patches the template
  via `store.update_skill`.
- **Inline-keyboard resume confirmation** — when a new task matches an
  interrupted one (Jaccard ≥ 0.7), the bot asks "Continue / Restart /
  Cancel" via Telegram inline buttons.
- **Live heartbeat** — every 60s the status message refreshes with
  "still working — *phase* (Xs)" when no event has come in.
- **Compact final summary card** — "✅ Done — `task_id` (12m 04s, 3
  sub-tasks, 8 iter, 2 replans, 14 files)".
- **DE prompts everywhere** — Planner / Reviewer / QuickReview /
  Triage all have full German system prompts when `lang=de`.

#### Infrastructure
- **Multi-plan voting** (opt-in via `cascade_multiplan_enabled`) — two
  plans in parallel at different temperatures, Sonnet picker.
- **Background chat-summariser** (`cascade/summarizer.py`) — every 6h,
  condenses old un-summarised messages into `chat_summaries` rows.
- **Repo-style probe** — when a task runs against an existing local
  repo, the bot reads pyproject.toml / .ruff.toml / .editorconfig /
  lockfiles and prepends the conventions to the planner's context.
- **Centralised logging** (`cascade/logging_config.py`) — rotating
  `debug.log` (gated on `CASCADE_DEBUG=1`) and always-on
  `telegram.log` audit trail.
- **Graceful shutdown** — `post_shutdown` marks running tasks as
  `interrupted` and waits up to 30s for in-flight handlers to flush
  before closing the DB. systemd `TimeoutStopSec=180`,
  `KillMode=mixed`.
- **`secrets.env` layering** — pydantic-settings now layers
  `.env` + `<CASCADE_HOME>/secrets.env`, with secrets taking
  precedence. The wizard writes only to the layer file.
- **MCP launcher options** — `scripts/mcp-launcher.sh` (auto-resolves
  pip / pipx / venv) and `npm/` wrapper for
  `claude mcp add cascade -- npx -y cascade-bot-mcp`.
- **RLM installer scripts** — `scripts/install-rlm-claude.sh` (Linux
  native) and `scripts/install-rlm-claude.ps1` (Windows → WSL).
- **`requirements.txt` / `requirements-dev.txt`** — for users who
  prefer pip-only over editable installs.
- **`/help` rewritten** — DE+EN reflect every new command and concept.

### Changed

- **Project renamed** to `cascade-bot-mcp` (was `claude-cascade`).
  Default branch renamed to `cascade-bot-mcp`.
- **`asyncio.TimeoutError` in claude_cli** now raises `RateLimitError`
  instead of `ClaudeCliError` so `with_retry` automatically retries
  instead of falling through to the regex heuristic.
- **`call_reviewer` / `call_planner`** now accept `lang=` and a `task=`
  parameter (reviewer); the prompt builders adapt accordingly.
- **`agent_chat`** accepts `retry_max_total_wait_s` /
  `retry_min_backoff_s` / `retry_max_backoff_s` for per-call retry
  tuning.

### Fixed

- **Drive-Setup amnesia** — the bug observed on 2026-04-26 ("Nein, ich
  habe keine JSON-Datei erhalten — du hast sie noch nicht geschickt"
  five minutes after a JSON upload) is fixed at three layers:
  chat_messages now persists file content + classification, the BM25
  recall finds short keywords like "json" / "drive", and the triage
  system prompt has explicit file-awareness rules.
- **Triage timeout fallback** — used to silently switch to the regex
  heuristic after a single 60s timeout; now retries with backoff and
  surfaces `waiting_for_session` if the wait exceeds the heartbeat
  threshold.
- **Resume corruption** — when iteration-0 plan JSON is malformed, the
  resume path now re-plans cleanly instead of crashing.
- **systemd SIGKILL** — used to lose state when the stop timeout
  (default 90s) elapsed. Now: graceful shutdown marks running tasks
  as `interrupted`, persists plan, then exits cleanly.

### Security

- `.env` and `secrets.env` are gitignored. The setup wizard never
  writes to `.env`.
- Direct-action target paths are validated against `_ALLOWED_ROOTS`
  before execution AND before being proposed to the user.
- File-content stored in `chat_messages.file_content` is treated as
  potentially-sensitive; `/forget` wipes it together with everything
  else for that chat.
- Pre-apply validation refuses to write Python with stub bodies so
  partial commits can't sneak past the reviewer.

---

## [0.1.0] — 2026-04-25

Initial public layout — Plan → Implement → Review loop, MCP server,
Telegram bot skeleton, basic memory.
