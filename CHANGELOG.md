# Changelog

All notable changes are listed here. Versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] — 2026-04-27

The "self-healing cascade" release. End-to-end live-driven hardening
of the orchestration loop based on a full day of real production
debugging on a representative multi-file Python refactor task. Headline
change: the cascade can now reliably finish complex tasks without
manual intervention — broken quality-checks self-repair, integration
review runs its own quality-checks, planner replans see the FULL
reviewer feedback (not a 300-byte truncation), and bot-restart
artefacts retry in 10s instead of 1h. Default implementer is
`kimi-k2.6` (April-2026 SWE-bench Verified leader at 80.2%).

### Added

#### Self-healing
- **Quality-check self-heal** (`cascade/check_repair.py`) — when a
  single check fails 3 iterations in a row, a focused planner-LLM call
  rewrites the check command (most common: missing `--exclude-dir=.venv`,
  wrong path scope, `python` vs `python3`). Each check gets one repair
  attempt; on failure the cascade falls through to a regular sub-task
  replan with a "drop the broken check" hint instead of hard-aborting.
- **Self-heal persistence** — repaired checks land in the iter-0 plan
  in DB so a `/resume` after a crash continues with the improved
  version. RLM-tagged for cross-task learning.
- **Integration-review self-heal** — the integration phase now mirrors
  the sub-task self-heal: runs plan-level quality_checks during review
  (was `check_results=None`), tracks per-check consecutive failures,
  triggers `repair_quality_check()` at the threshold, persists the
  repaired plan into iter-0.
- **Sub-task hard-abort fallthrough** — when replan budget exhausts
  and the same check still fails, the cascade hard-fails with a
  precise "stuck-check" diagnosis naming the offending check; before
  this the run looped to `cascade_max_iterations`.
- **Implementer-stuck auto-replan** — `HealingMonitor` flags 3 identical
  implementer-output hashes; the cascade reads the flag and forces
  a replan instead of letting the same diff echo forever.
- **Empty-ops loop-breaker** — 2 consecutive `ops=[]` outputs from
  the implementer trigger a forced replan instead of waiting for the
  reviewer-feedback stagnation detector.

#### Reliability + retry
- **Infinite retry on cloud-LLM errors** — ALL Ollama / Claude API /
  OpenAI-compatible errors now retry indefinitely (1h fixed backoff,
  7-day budget) until upstream recovers. Permanent config errors
  (no API key, missing CLI binary) still raise immediately.
- **Short-backoff for fast-recovery signals** — `exited 143/137`
  (SIGTERM/SIGKILL), connection-reset/refused, timeouts, HTTP
  500/502/504 → 10–30 s clamp instead of the 1h floor. A bot-restart
  artefact no longer wedges the cascade for an hour.
- **Empty-error fingerprint** — `_ollama_cloud_chat` now mines
  `type(e).__name__` + `status_code` from exceptions whose `str(e)`
  is empty, so the short-backoff classifier can match them.
- **JSON-repair pass for planner + reviewer** — same trick the
  implementer had for months: on parse failure, ask the same model to
  fix its own broken output with `temperature=0.0` and a schema-aware
  repair prompt. Saves the run from a single-blip JSON corruption.
- **30-min implementer/agent HTTP timeout** (was 600s) — kimi-k2.6
  with xhigh effort on big plugin refactors needs the headroom.
- **`/cancel`-keyboard on hard-stuck** — when no progress event has
  fired for 5 minutes the bot surfaces an inline-keyboard
  `✋ Abbrechen / ⏳ Weiter warten` so the user doesn't have to type
  `/cancel <id>` to break out of a wedged retry-sleep.

#### Plan / replan quality
- **Planner prompt explicitly forbids `.venv` greps** — the most
  common broken-check class. Now the prompt mandates
  `--exclude-dir={.venv,venv,__pycache__,node_modules,dist,build,.git}`
  on every `grep`/`find`, and `python3 -m py_compile` over `python -c
  "import …"` for sub-package imports.
- **Multi-plan voting on REPLANS** (was: only initial plan) — when
  `cascade_multiplan_enabled=True`, replans now run two competing
  planner calls and pick the better one via Sonnet. Sub-task replan
  is exactly where the second opinion helps most.
- **Full reviewer feedback in replan prompt** — was 300-char-truncated
  per iteration, now 4 kB for the latest iter (older iters stay at
  300 chars as context). The planner now sees the reviewer's full
  fix-list instead of just the symptom-summary.
- **Severity-aware replan trigger** — `review.severity == "high"`
  bumps `consecutive_failures` to the replan threshold immediately,
  skipping the normal "wait for 2 fails" period.
- **Plan validation at entry** — empty plans (all of direct_ops,
  subtasks, steps, files_to_touch, acceptance_criteria empty) get
  one retry; if still empty the run fails before workspace setup.

#### Reviewer rigor
- **Explicit per-criterion verification** — `ReviewResult` now carries
  both `passing_criteria` and `failing_criteria`. The reviewer prompt
  mandates the two lists together cover every entry of
  `plan.acceptance_criteria` — no silent skipping. Vibes-pass and
  vibes-fail are gone.

#### Feedback-driven context
- **Reviewer-named files auto-included** — a new helper
  `_extract_paths_from_feedback()` regex-extracts file paths from
  reviewer text (e.g. `tests/test_smoke.py`) and adds them to the
  next implementer call's `EXISTING FILE CONTENTS`. Persists across
  replan boundaries so the first post-replan iter still gets the
  context it needs.

#### Final quality gate
- **Re-run plan.quality_checks before stamping `done`** — caught the
  rare case where a later sub-task or integration-repair broke an
  earlier sub-task's invariant. Failing gate flips status to `failed`
  with the offending check names in the summary.

#### Watchdog + UX
- **`hard_stuck` ProgressEvent** — `HealingMonitor` emits this after
  300 s of NO progress event (was: only logged a warning at 180 s).
  Bot renders an inline keyboard so the user can decide to abort
  rather than wait silently for a 1h retry-sleep.
- **`/cancel <id>` works for orphan tasks** — sweeps DB-running tasks
  even when the in-process `INFLIGHT` slot was overwritten by a newer
  task in the same chat. New `TASK_REGISTRY` keyed by task-id.
- **`/abort` mirrors `/cancel` for ALL tasks** — including a DB-sweep
  for orphan running/queued/interrupted rows from previous bot crashes.
- **Cancel-intent guard** — "abbrechen" / "abort the task" / `/cancel`
  / `/abort` are now caught BEFORE the triage LLM. Was: the LLM
  routed them as a new cascade ("cancel task X" → triage thinks it's
  a coding job → infinite meta-loop). Pre-LLM regex skips that path.
- **Live-switch tip on wait-for-session** — the hard_stuck keyboard
  message now includes `/cancel <id>` → `/models` → `/resume <id>` so
  the user can swap implementer mid-run without remembering the dance.

#### Logging + observability
- **`ITER_TIMING` per sub-task iter** — one INFO log line per iter
  end with task / iter / subtask / total_s / ops / failed_ops /
  checks_pass / reviewer_pass. Sortable.
- **`RUN_SUMMARY` JSON-line per terminal** — emits at every done /
  failed / cancelled return. `journalctl … | grep RUN_SUMMARY | jq`
  pipes one row per run for cross-run analysis. Fields: status, iters,
  duration, replans, integration-repairs, self-heal-repairs,
  files_changed, plan_summary, sub_tasks, plan_checks.
- **Healing log demoted to DEBUG** — "still within tolerance" was
  spamming 12+ INFO lines per minute during normal long LLM calls.
  The actual stuck-alert at 180 s is still WARNING + user-visible.

#### Defaults
- **Default implementer model: `kimi-k2.6`** (was `qwen3-coder:480b`)
  — top of SWE-bench Verified April 2026 at 80.2 %, near-doubled
  tool-calling reliability vs k2.5. Both available on Ollama Cloud,
  same API path.
- **Configurable wait-cap** — new `cascade_max_wait_s` setting (default
  still 7 days). Triage explicitly overrides to 180 s so chat stays
  responsive even when an LLM upstream is flapping.
- **Workspace disk-quota** — new `cascade_workspace_max_bytes` (default
  1 GB). Aborts the run if the implementer goes runaway-write before
  the disk fills.
- **systemd `TimeoutStopSec=180` + `KillMode=mixed`** — 90 s default
  was killing the bot mid-cascade with SIGKILL; now `post_shutdown`
  has a fair window to mark running tasks as `interrupted`.

#### Op validation
- **Pre-apply rejection for empty `content=""` writes**, duplicate
  writes to the same path in one batch, and no-op edits where
  `find == replace`. Implementer can no longer silently "succeed" with
  garbage that the reviewer then has to reject.

### Fixed
- Reviewer JSON corruption no longer crashes the run (JSON-repair pass).
- `INFLIGHT[chat]` overwrite race fixed via `TASK_REGISTRY[task_id]`.
- `/stop` was removed; `/cancel <id>` and `/abort` cover all use cases.
- `_healing_progress` no longer resets the idle timer on its own emits
  (`hard_stuck` was triggering itself in a loop).
- `cascade.core` `r.detail` → `r.output` typo on `CheckResult`
  (introduced and fixed same day).

### Tests
- 457 / 457 green (was 444 in 0.2.0). New coverage for: short-backoff
  patterns, op-validation rejections, plan validation, empty-ops
  loop-breaker, final quality gate, self-heal-repair persistence,
  implementer-stuck auto-replan.

### Migration
- No breaking config changes. Existing `.env` continues to work; new
  settings (`cascade_max_wait_s`, `cascade_workspace_max_bytes`) have
  sensible defaults. The default implementer model swap from
  `qwen3-coder:480b` to `kimi-k2.6` only takes effect for tasks
  created AFTER upgrade — existing tasks keep their snapshotted model.

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
