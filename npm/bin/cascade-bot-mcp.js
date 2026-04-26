#!/usr/bin/env node
/**
 * npx-wrapper for Cascade-Bot's MCP server.
 *
 * The actual MCP server is a Python program. This wrapper exists so
 * Claude Code can install/run it with the same UX as pure-JS MCP
 * servers:
 *
 *     claude mcp add cascade -- npx -y cascade-bot-mcp
 *
 * Resolution order matches scripts/mcp-launcher.sh:
 *   1. $CASCADE_HOME/.venv/bin/python + mcp_server.py
 *   2. cascade-mcp (entry-point script from pip install)
 *   3. python3 mcp_server.py (last-ditch)
 *
 * If none of those work we print a clear, copy-pasteable install hint.
 */

"use strict";

const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

function fileExists(p) {
  try { return fs.statSync(p).isFile(); } catch { return false; }
}
function execExists(p) {
  try {
    const st = fs.statSync(p);
    return st.isFile() && (st.mode & 0o111);
  } catch { return false; }
}
function which(cmd) {
  const PATH = process.env.PATH || "";
  for (const dir of PATH.split(path.delimiter)) {
    const p = path.join(dir, cmd);
    if (execExists(p)) return p;
  }
  return null;
}

const CASCADE_HOME = process.env.CASCADE_HOME
  || path.join(os.homedir(), "claude-cascade");

let candidates = [];

const venvPy = path.join(CASCADE_HOME, ".venv", "bin", "python");
const repoMain = path.join(CASCADE_HOME, "mcp_server.py");
if (execExists(venvPy) && fileExists(repoMain)) {
  candidates.push({ cmd: venvPy, args: [repoMain] });
}

const venvBin = path.join(CASCADE_HOME, ".venv", "bin", "cascade-mcp");
if (execExists(venvBin)) {
  candidates.push({ cmd: venvBin, args: [] });
}

const sysCascadeMcp = which("cascade-mcp");
if (sysCascadeMcp) {
  candidates.push({ cmd: sysCascadeMcp, args: [] });
}

const sysPy = which("python3") || which("python");
if (sysPy && fileExists(repoMain)) {
  candidates.push({ cmd: sysPy, args: [repoMain] });
}

if (candidates.length === 0) {
  process.stderr.write(
    [
      "[cascade-bot-mcp] no Python entry-point found.",
      "",
      "Install one of:",
      `  • git clone + venv:  git clone https://github.com/CTreitges/cascade-bot-mcp '${CASCADE_HOME}'`,
      `                      cd '${CASCADE_HOME}' && python3 -m venv .venv`,
      `                      .venv/bin/pip install -e .`,
      `  • pipx:             pipx install cascade-bot-mcp`,
      "",
      "Then re-run.  Set CASCADE_HOME if you used a different path.",
      "",
    ].join("\n")
  );
  process.exit(2);
}

const { cmd, args } = candidates[0];
const child = spawn(cmd, args, { stdio: "inherit" });
child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
  } else {
    process.exit(code ?? 0);
  }
});
process.on("SIGINT",  () => child.kill("SIGINT"));
process.on("SIGTERM", () => child.kill("SIGTERM"));
