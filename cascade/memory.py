"""Cross-task memory: best-effort persistence of decisions / findings / facts
that should outlive a single cascade run.

Two backends, used in this priority order:
  1. HTTP — if `RLM_HTTP_ENDPOINT` is set, POST {category, content, tags,
     importance, project} to that URL. Used when an RLM-server is running
     side-by-side and exposes /remember.
  2. Local JSONL — always-on fallback. Append one JSON object per line to
     `<CASCADE_HOME>/store/memory.jsonl`. This is the durable record even
     when nothing else is reachable, and can be replayed into a real RLM
     later.

Reads (`recall_context`) walk the local JSONL backwards looking for tagged
entries whose tag-set intersects the query keywords.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Literal

from .config import settings

log = logging.getLogger("cascade.memory")

PROJECT = "claude-cascade"


def _memory_path() -> Path:
    s = settings()
    p = s.cascade_home / "store" / "memory.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


async def _http_post(url: str, body: dict, *, timeout_s: float = 10) -> bool:
    """POST a memory entry to an external RLM endpoint. Returns True on 2xx."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.post(url, json=body)
        return 200 <= r.status_code < 300
    except Exception as e:
        log.debug("rlm http post failed: %s", e)
        return False


def _append_jsonl(entry: dict) -> bool:
    """Synchronous, append-only — runs in to_thread."""
    path = _memory_path()
    line = json.dumps(entry, ensure_ascii=False, default=str)
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except Exception as e:
        log.warning("memory jsonl append failed: %s", e)
        return False


async def remember_finding(
    content: str,
    *,
    category: Literal["finding", "decision", "preference", "fact"] = "finding",
    importance: Literal["low", "medium", "high", "critical"] = "medium",
    tags: str = "claude-cascade",
    extra: dict[str, Any] | None = None,
) -> bool:
    """Record an insight. Best-effort: never raises, returns True iff at least
    one backend succeeded."""
    entry = {
        "ts": time.time(),
        "project": PROJECT,
        "category": category,
        "importance": importance,
        "tags": tags,
        "content": content,
        **(extra or {}),
    }

    ok = False

    # 1) external RLM via HTTP (if configured)
    url = os.getenv("RLM_HTTP_ENDPOINT")
    if url:
        if await _http_post(url, entry):
            ok = True

    # 2) local JSONL (always)
    if await asyncio.to_thread(_append_jsonl, entry):
        ok = True

    if ok:
        log.info("memory[%s/%s] %s", category, importance, content[:120])
    return ok


async def remember_decision(content: str, **kw: Any) -> bool:
    return await remember_finding(content, category="decision", **kw)


async def remember_fact(content: str, **kw: Any) -> bool:
    return await remember_finding(content, category="fact", **kw)


async def cleanup_old_entries(*, retention_days: int = 90) -> int:
    """Trim the memory.jsonl: drop entries older than retention_days.
    Returns count removed. No-op if file doesn't exist."""
    path = _memory_path()
    if not path.exists():
        return 0

    def _do() -> int:
        cutoff = time.time() - retention_days * 86400
        kept: list[str] = []
        removed = 0
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except Exception:
                        kept.append(line.rstrip("\n"))
                        continue
                    if (e.get("ts") or 0) >= cutoff:
                        kept.append(line.rstrip("\n"))
                    else:
                        removed += 1
            if removed:
                path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        except Exception as e:
            log.warning("memory cleanup failed: %s", e)
        return removed

    return await asyncio.to_thread(_do)


async def recall_context(task: str, *, limit: int = 3) -> str | None:
    """Look up recent memory entries whose tags or content overlap with the
    task. Returns a short bullet-list of recalls, or None if nothing useful.
    """
    path = _memory_path()
    if not path.exists():
        return None

    def _scan() -> list[dict]:
        keywords = {w.lower() for w in task.split() if len(w) > 4}
        out: list[dict] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    haystack = (e.get("content", "") + " " + e.get("tags", "")).lower()
                    if any(k in haystack for k in keywords):
                        out.append(e)
        except Exception:
            return []
        return out[-limit:]

    matches = await asyncio.to_thread(_scan)
    if not matches:
        return None
    lines = []
    for e in matches:
        cat = e.get("category", "?")
        imp = e.get("importance", "?")
        content = (e.get("content") or "")[:200]
        lines.append(f"  [{cat}/{imp}] {content}")
    return "\n".join(lines)
