"""Brave Search Web API client.

Triggered on demand by cascade/research.py when an incoming task likely
needs current real-world information that Context7 (library docs) can't
provide — pricing, current dates, recent events, vendor announcements.

Requires `BRAVE_SEARCH_API_KEY` in the env. Without a key the helper
returns None silently — the rest of the cascade carries on as before.

Endpoint: GET https://api.search.brave.com/res/v1/web/search
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger("cascade.web_search")

_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_TIMEOUT = 10.0


def _api_key() -> str | None:
    key = os.getenv("BRAVE_SEARCH_API_KEY") or ""
    return key.strip() or None


async def search(
    query: str, *, count: int = 5, country: str = "DE", lang: str = "de"
) -> list[dict[str, Any]]:
    """Run a Brave web search. Returns up to `count` hits with title/url/desc.

    No key configured → []. HTTP / parse errors → []. Never raises."""
    key = _api_key()
    if not key or not query.strip():
        return []
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": key,
    }
    params = {
        "q": query,
        "count": str(count),
        "country": country,
        "search_lang": lang,
        "safesearch": "moderate",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(_ENDPOINT, headers=headers, params=params)
        if r.status_code != 200:
            log.debug("brave search non-200: %s body=%s", r.status_code, r.text[:200])
            return []
        data = r.json()
        results = (data.get("web") or {}).get("results") or []
        out = []
        for h in results[:count]:
            out.append(
                {
                    "title": (h.get("title") or "").strip(),
                    "url": (h.get("url") or "").strip(),
                    "description": (h.get("description") or "").strip(),
                }
            )
        return out
    except Exception as e:
        log.debug("brave search failed: %s", e)
        return []


def is_configured() -> bool:
    return bool(_api_key())
