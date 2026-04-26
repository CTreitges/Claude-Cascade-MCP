#!/usr/bin/env bash
# Install RLM-Claude (long-term memory backend) — Linux native or WSL2.
#
# Usage:
#   bash scripts/install-rlm-claude.sh
#
# What it does:
#   1. Checks Python ≥ 3.11.
#   2. Installs `rlm-claude` into the active venv (or pipx if no venv).
#   3. Initialises the RLM data dir (~/.rlm-claude/).
#   4. Registers the MCP server with the local `claude` CLI so the
#      Cascade-Bot can recall+remember across runs.
#
# Cascade itself runs without RLM — recall just falls back to the local
# `store/memory.jsonl`. Installing RLM unlocks BM25 ranking + cross-host
# sync for power users.
set -euo pipefail

YELLOW=$'\033[33m'
GREEN=$'\033[32m'
RED=$'\033[31m'
RESET=$'\033[0m'

echo "${YELLOW}== Cascade-Bot: RLM-Claude installer ==${RESET}"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "${RED}python3 not found on PATH.${RESET}"
  exit 1
fi
PY_VER=$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
case "$PY_VER" in
  3.1[1-9]|3.2[0-9]) ;;
  *)
    echo "${RED}Python 3.11+ required, found $PY_VER.${RESET}"
    exit 1
    ;;
esac
echo "Python: $PY ($PY_VER)"

if [ -n "${VIRTUAL_ENV:-}" ]; then
  echo "Active venv: $VIRTUAL_ENV"
  INSTALL_CMD=(pip install --upgrade rlm-claude)
elif command -v pipx >/dev/null 2>&1; then
  echo "Using pipx (no venv active)"
  INSTALL_CMD=(pipx install rlm-claude)
else
  echo "${YELLOW}No active venv and no pipx — falling back to user pip install.${RESET}"
  echo "${YELLOW}Tip: source your venv first or 'sudo apt install pipx'.${RESET}"
  INSTALL_CMD=("$PY" -m pip install --user --upgrade rlm-claude)
fi

echo
echo "${YELLOW}Step 1/3: install rlm-claude${RESET}"
"${INSTALL_CMD[@]}"

# Verify it landed somewhere on PATH (or in user-base scripts).
if ! command -v rlm-claude >/dev/null 2>&1; then
  USER_BIN="$("$PY" -c 'import site; print(site.USER_BASE)')/bin"
  if [ -x "$USER_BIN/rlm-claude" ]; then
    echo "${YELLOW}rlm-claude installed under $USER_BIN/ — add it to PATH.${RESET}"
    export PATH="$USER_BIN:$PATH"
  else
    echo "${RED}rlm-claude command still not found on PATH after install.${RESET}"
    exit 1
  fi
fi

echo
echo "${YELLOW}Step 2/3: initialise RLM data dir${RESET}"
rlm-claude init || echo "${YELLOW}rlm-claude init returned non-zero (already initialised?).${RESET}"

echo
echo "${YELLOW}Step 3/3: register MCP server with claude CLI${RESET}"
if command -v claude >/dev/null 2>&1; then
  claude mcp add rlm-claude --scope user -- rlm-claude serve \
    && echo "${GREEN}✓ rlm-claude registered with claude CLI${RESET}" \
    || echo "${YELLOW}claude mcp add returned non-zero (already registered?).${RESET}"
else
  cat <<'EOF'
claude CLI not found on PATH — skipping MCP registration.

Once you install Claude Code and run `claude --version`, register manually:
  claude mcp add rlm-claude --scope user -- rlm-claude serve
EOF
fi

echo
echo "${GREEN}== Done. ==${RESET}"
echo "Test: rlm-claude status"
echo "Bot will pick RLM up automatically on next start."
