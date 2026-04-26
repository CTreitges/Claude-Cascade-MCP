"""Read/write the user's local `secrets.env` file.

The setup wizard (`/setup` in Telegram, `cascade --setup` in the CLI)
writes individual `KEY=VALUE` lines here so the user's hand-edited
`.env` is never overwritten. The file is gitignored by default.

Format: simple POSIX-ish env file. Each line is `KEY=value` with no
quoting; values are written verbatim (no spaces around the `=`). This
keeps the file readable for humans without a shell-quoting parser.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger("cascade.secrets_store")


_VALID_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _default_path() -> Path:
    home = os.environ.get("CASCADE_HOME") or str(Path.home() / "claude-cascade")
    return Path(home) / "secrets.env"


def secrets_path() -> Path:
    """Where `set_secret` writes. Honours `CASCADE_HOME` (so the bot's
    runtime layout matches the wizard's writes)."""
    return _default_path()


def load_secrets(path: Path | None = None) -> dict[str, str]:
    """Read existing entries. Lines starting with `#`, blank lines, and
    malformed lines (no `=`) are skipped. Returns the most recent value
    for each KEY (later lines win — matches what `read_text` semantics
    a user expects when editing by hand)."""
    p = path or _default_path()
    if not p.is_file():
        return {}
    out: dict[str, str] = {}
    try:
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if not _VALID_KEY.match(k):
                continue
            out[k] = v
    except Exception as e:
        log.warning("could not read %s: %s", p, e)
    return out


def set_secret(key: str, value: str, *, path: Path | None = None) -> Path:
    """Set or update KEY in secrets.env. Creates the file (and parent
    dir) if missing. chmod 600 so credentials don't leak to other users
    on the box. Returns the resolved path so callers can show it back
    to the user."""
    if not _VALID_KEY.match(key):
        raise ValueError(f"invalid env key: {key!r} (must be UPPERCASE_SNAKE)")
    p = path or _default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    existing: list[str] = []
    if p.is_file():
        try:
            existing = p.read_text(encoding="utf-8").splitlines()
        except Exception:
            existing = []
    new_line = f"{key}={value}"
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    found = False
    out_lines: list[str] = []
    for ln in existing:
        if pattern.match(ln):
            if not found:
                out_lines.append(new_line)
                found = True
            # skip duplicate copies of this key
        else:
            out_lines.append(ln)
    if not found:
        if out_lines and out_lines[-1].strip():
            out_lines.append("")
        out_lines.append(new_line)
    p.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass  # chmod fails on some FS / Windows — secrets.env stays
    return p


def unset_secret(key: str, *, path: Path | None = None) -> bool:
    """Remove KEY from secrets.env. Returns True if a row was removed."""
    p = path or _default_path()
    if not p.is_file():
        return False
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return False
    kept = [ln for ln in lines if not pattern.match(ln)]
    if len(kept) == len(lines):
        return False
    p.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return True
