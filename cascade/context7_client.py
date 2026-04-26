"""Context7 public-API client.

Used by cascade/research.py to fetch up-to-date library docs for the
Planner / Implementer / Reviewer / Triage prompts on demand.

Two endpoints, both anonymous (no auth required):
  - GET  https://context7.com/api/v1/search?query=<q>&limit=<n>   → library hits
  - GET  https://context7.com/api/v1/<id>?topic=<x>&tokens=<n>    → markdown docs

All calls are best-effort: timeouts/errors return None and never raise.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("cascade.context7")

_BASE = "https://context7.com/api/v1"
_TIMEOUT = 8.0


async def search_libraries(query: str, *, limit: int = 3) -> list[dict[str, Any]]:
    """Search Context7's index. Returns ranked hits (id, title, score, …)."""
    if not query.strip():
        return []
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(
                f"{_BASE}/search",
                params={"query": query, "limit": limit},
            )
        if r.status_code != 200:
            log.debug("context7 search non-200: %s", r.status_code)
            return []
        data = r.json()
        return list(data.get("results") or [])[:limit]
    except Exception as e:
        log.debug("context7 search failed: %s", e)
        return []


async def fetch_docs(library_id: str, *, topic: str = "", tokens: int = 3000) -> str | None:
    """Pull markdown docs for a resolved library_id (e.g. '/pydantic/pydantic')."""
    if not library_id.startswith("/"):
        library_id = "/" + library_id
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(
                f"{_BASE}{library_id}",
                params={"topic": topic, "tokens": tokens, "type": "txt"},
            )
        if r.status_code != 200:
            log.debug("context7 docs %s non-200: %s", library_id, r.status_code)
            return None
        text = r.text or ""
        return text.strip() or None
    except Exception as e:
        log.debug("context7 docs %s failed: %s", library_id, e)
        return None


async def docs_for_query(query: str, *, topic: str = "", tokens: int = 3000) -> str | None:
    """Convenience: search → take top hit → fetch its docs.

    Returns a single markdown blob or None.
    """
    hits = await search_libraries(query, limit=1)
    if not hits:
        return None
    top = hits[0]
    lib_id = top.get("id")
    if not lib_id:
        return None
    body = await fetch_docs(lib_id, topic=topic, tokens=tokens)
    if not body:
        return None
    title = top.get("title") or lib_id
    return f"# {title}  ({lib_id})\n\n{body}"
