"""Probe an existing repo for stylistic conventions.

When `Plan.repo.kind == 'local'`, the cascade has access to the repo
on disk. This module pulls a few cheap signals out of it so the
Planner and Implementer can match the existing style instead of
imposing whatever defaults their training data has:

  - line length / indent (from .ruff.toml, ruff section in pyproject.toml,
    .editorconfig, .black config)
  - package manager (poetry vs pip vs uv vs hatch)
  - which test runner is configured (pytest options block in pyproject)
  - whether type-checking (mypy / pyright) is set up

The result is a short markdown block ready to splice into the planner /
implementer prompts.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger("cascade.style_probe")


def probe_repo_style(repo_path: Path) -> dict:
    """Return a flat dict of style hints for `repo_path`. Keys are short
    identifiers, values are strings (or lists of strings). Missing
    signals just don't appear in the dict.

    Best-effort: every read is wrapped in try/except — a half-broken
    repo never crashes the probe.
    """
    hints: dict = {}
    if not repo_path or not repo_path.is_dir():
        return hints

    # --- pyproject.toml ---
    pyproject = repo_path / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = _safe_load_toml(pyproject)
            tool = (data or {}).get("tool", {}) if isinstance(data, dict) else {}

            # Build backend / package manager
            build = (data or {}).get("build-system", {}).get("requires") or []
            for b in build:
                low = str(b).lower()
                if "poetry" in low:
                    hints["package_manager"] = "poetry"
                    break
                if low.startswith("hatchling"):
                    hints["package_manager"] = "hatch"
                    break
                if low.startswith("setuptools"):
                    hints.setdefault("package_manager", "pip+setuptools")
            if "poetry" in tool:
                hints["package_manager"] = "poetry"

            # ruff config
            ruff_cfg = tool.get("ruff", {}) if isinstance(tool, dict) else {}
            ll = ruff_cfg.get("line-length")
            if ll:
                hints["line_length"] = str(ll)
            sel = ruff_cfg.get("lint", {}).get("select") or ruff_cfg.get("select")
            if sel:
                hints["ruff_select"] = ",".join(str(x) for x in sel[:8])

            # black
            black_cfg = tool.get("black", {}) if isinstance(tool, dict) else {}
            ll_b = black_cfg.get("line-length")
            if ll_b and "line_length" not in hints:
                hints["line_length"] = str(ll_b)

            # pytest
            pytest_cfg = tool.get("pytest", {}).get("ini_options", {}) if isinstance(tool, dict) else {}
            if pytest_cfg:
                hints["test_runner"] = "pytest"
                addopts = pytest_cfg.get("addopts")
                if addopts:
                    hints["pytest_addopts"] = str(addopts)[:120]

            # mypy / pyright
            if tool.get("mypy"):
                hints["typecheck"] = "mypy"
            if tool.get("pyright"):
                hints["typecheck"] = (
                    "pyright" if "typecheck" not in hints else f"{hints['typecheck']}+pyright"
                )
        except Exception as e:
            log.debug("pyproject probe failed: %s", e)

    # --- standalone .ruff.toml ---
    ruff_toml = repo_path / ".ruff.toml"
    if ruff_toml.is_file():
        try:
            data = _safe_load_toml(ruff_toml)
            if isinstance(data, dict):
                ll = data.get("line-length")
                if ll:
                    hints["line_length"] = str(ll)
        except Exception:
            pass

    # --- .editorconfig (very simple key-grep) ---
    ec = repo_path / ".editorconfig"
    if ec.is_file():
        try:
            txt = ec.read_text(encoding="utf-8", errors="replace")[:8000]
            m = re.search(r"indent_size\s*=\s*(\d+)", txt)
            if m:
                hints.setdefault("indent_size", m.group(1))
            m = re.search(r"max_line_length\s*=\s*(\d+)", txt)
            if m and "line_length" not in hints:
                hints["line_length"] = m.group(1)
        except Exception:
            pass

    # --- requirements / lockfiles ---
    if (repo_path / "uv.lock").exists():
        hints["package_manager"] = "uv"
    elif (repo_path / "poetry.lock").exists():
        hints["package_manager"] = "poetry"
    elif (repo_path / "Pipfile.lock").exists():
        hints["package_manager"] = "pipenv"

    return hints


def format_style_hints(hints: dict, *, lang: str = "en") -> str | None:
    """Render the dict from `probe_repo_style` as a markdown block.
    Returns None when nothing was discovered."""
    if not hints:
        return None
    if lang == "de":
        head = "=== REPO-STIL (auto-erkannt) ==="
        notes = [
            "Halte dich an diese Konventionen, wenn nichts dagegen spricht:",
        ]
    else:
        head = "=== REPO STYLE (auto-detected) ==="
        notes = [
            "Match these conventions unless the task explicitly asks otherwise:",
        ]
    bullets = []
    label_map = {
        "line_length": "line length",
        "indent_size": "indent size",
        "ruff_select": "ruff lint select",
        "package_manager": "package manager",
        "test_runner": "test runner",
        "pytest_addopts": "pytest addopts",
        "typecheck": "type checker",
    }
    for k, v in hints.items():
        bullets.append(f"  - {label_map.get(k, k)}: `{v}`")
    return head + "\n" + "\n".join(notes + bullets)


def _safe_load_toml(path: Path):
    """tomllib is stdlib in 3.11+. Returns None on any error."""
    try:
        import tomllib
        with path.open("rb") as f:
            return tomllib.load(f)
    except Exception:
        return None
