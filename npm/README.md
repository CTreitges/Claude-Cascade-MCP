# cascade-bot-mcp (npm wrapper)

Tiny Node-based launcher so Claude Code can install Cascade-Bot's MCP
server via the standard `npx` flow:

```bash
claude mcp add cascade -- npx -y cascade-bot-mcp
```

The wrapper finds the Python MCP server in this resolution order:

1. `$CASCADE_HOME/.venv/bin/python` + `$CASCADE_HOME/mcp_server.py`
2. `$CASCADE_HOME/.venv/bin/cascade-mcp` (entry-point script)
3. `cascade-mcp` on PATH (pipx / system pip install)
4. `python3 mcp_server.py`

If none of these are present it prints a copy-pasteable install hint.

This package contains **no Python code itself** — it just bridges the
npm/npx ecosystem to the Python implementation in the parent repo.
Publish from the repo root via:

```bash
cd npm && npm publish --access public
```

The Python part still has to be installed separately (git clone + venv,
or `pipx install cascade-bot-mcp`).
