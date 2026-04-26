#!/usr/bin/env bash
# MCP-launcher for Cascade-Bot's stdio MCP server.
#
# Goal: a one-liner the user can paste into `claude mcp add` no matter
# whether the bot is installed via pip / pipx / cloned repo / WSL.
#
# Usage from claude CLI:
#   claude mcp add cascade -- bash <(curl -sSL .../mcp-launcher.sh)
#
# Or after `git clone`:
#   claude mcp add cascade -- bash /path/to/scripts/mcp-launcher.sh
#
# Resolution order:
#   1. CASCADE_HOME/.venv/bin/python  (preferred — uses the bot's deps)
#   2. PYTHONPATH override + system python3
#   3. pipx-installed cascade-mcp     (Last fallback if user pipx'd it)
set -euo pipefail

# Find the cascade install root.
CASCADE_HOME="${CASCADE_HOME:-$HOME/claude-cascade}"

# Option 1: editable / git checkout with venv
if [ -x "$CASCADE_HOME/.venv/bin/python" ] && [ -f "$CASCADE_HOME/mcp_server.py" ]; then
    exec "$CASCADE_HOME/.venv/bin/python" "$CASCADE_HOME/mcp_server.py"
fi

# Option 2: same venv, different layout
if [ -x "$CASCADE_HOME/.venv/bin/cascade-mcp" ]; then
    exec "$CASCADE_HOME/.venv/bin/cascade-mcp"
fi

# Option 3: pipx / system install
if command -v cascade-mcp >/dev/null 2>&1; then
    exec cascade-mcp
fi

# Option 4: bare python3 + repo on PYTHONPATH
if [ -f "$CASCADE_HOME/mcp_server.py" ]; then
    exec python3 "$CASCADE_HOME/mcp_server.py"
fi

echo "Cascade-Bot MCP server not found." >&2
echo "Set CASCADE_HOME to the cascade-bot-mcp install dir, or" >&2
echo "  pipx install cascade-bot-mcp" >&2
exit 1
