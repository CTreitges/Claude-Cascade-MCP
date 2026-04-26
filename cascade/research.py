"""On-demand external context gathering: Context7 docs + Brave web search.

Called once at the start of a cascade run (see cascade/core.py) and the
result string is appended to the Planner / Implementer / Reviewer / Triage
prompts as a `=== EXTERNAL CONTEXT ===` block. The agents see fresh
library docs and live web hits without having to call MCP themselves —
which means it works equally for Claude AND Ollama backends.

Two trigger heuristics:

  - *Library detection* → Context7. Scans the task text for known
    library/framework keywords (pydantic, fastapi, click, openpyxl, react …)
    plus a generic "import X" / "from X" pattern, then pulls docs for the
    top match(es).

  - *Web-search trigger* → Brave. Fired when the task mentions live-fact
    keywords (preise, aktuell, latest, neueste, news, version, release,
    today, heute, …) — content that won't be in a library index but might
    be on the live web.

Both calls are best-effort: timeouts, missing API keys, parse errors
all degrade silently to "no extra context".
"""

from __future__ import annotations

import asyncio
import logging
import re

from .context7_client import docs_for_query
from .web_search import is_configured as brave_is_configured
from .web_search import search as brave_search

log = logging.getLogger("cascade.research")


# Curated keyword list — pragmatic, not exhaustive. Ordered roughly by
# popularity in the codebases we expect to touch (Python-heavy + some web).
_LIBRARY_KEYWORDS = (
    # python data / web
    "pydantic", "fastapi", "flask", "django", "starlette", "sqlalchemy",
    "alembic", "celery", "httpx", "requests", "aiohttp", "uvicorn",
    "openpyxl", "pandas", "numpy", "polars", "pyarrow", "duckdb",
    "click", "typer", "argparse", "rich",
    "pytest", "pydantic-settings", "ruff", "mypy", "black",
    # ai / llm
    "anthropic", "openai", "ollama", "langchain", "llama-index",
    "langgraph", "litellm", "transformers",
    # telegram / bots
    "python-telegram-bot", "aiogram", "telethon",
    # data viz / docs
    "matplotlib", "plotly", "streamlit", "gradio", "mkdocs",
    # web frontend
    "react", "next.js", "nextjs", "vue", "svelte", "tailwind", "tailwindcss",
    "vite", "webpack", "typescript",
    # node
    "express", "fastify", "nestjs", "prisma",
    # infra
    "docker", "kubernetes", "terraform", "ansible",
)

# Imperfect but useful: low-FP web-trigger words.
_WEB_TRIGGERS_DE = re.compile(
    r"\b(aktuell|aktuelle|neueste|neuester|news|version|release|"
    r"preis|preise|heute|gestern|morgen|stand|datum|"
    r"vergleich|vergleiche)\b",
    re.IGNORECASE,
)
_WEB_TRIGGERS_EN = re.compile(
    r"\b(latest|newest|recent|news|current|today|yesterday|tomorrow|"
    r"price|pricing|release|version|compare|comparison)\b",
    re.IGNORECASE,
)


def detect_libraries(text: str, *, max_hits: int = 3) -> list[str]:
    """Return a deduped list of library names mentioned in `text`.

    Uses two signals:
      1. exact keyword hits from the curated list (case-insensitive)
      2. `import X` / `from X import` source-style mentions
    """
    if not text:
        return []
    seen: list[str] = []
    low = text.lower()
    for lib in _LIBRARY_KEYWORDS:
        if lib.lower() in low and lib not in seen:
            seen.append(lib)
        if len(seen) >= max_hits:
            return seen[:max_hits]
    for m in re.finditer(
        r"(?:^|\s)(?:import|from)\s+([A-Za-z][A-Za-z0-9_\-\.]{1,30})",
        text,
    ):
        cand = m.group(1).split(".")[0]
        if cand and cand not in seen and len(seen) < max_hits:
            seen.append(cand)
    return seen[:max_hits]


def needs_web_search(text: str, lang: str = "de") -> bool:
    if not text:
        return False
    pattern = _WEB_TRIGGERS_DE if lang == "de" else _WEB_TRIGGERS_EN
    return bool(pattern.search(text))


def _format_web_hits(hits: list[dict], lang: str) -> str | None:
    if not hits:
        return None
    header = "## 🌐 Web-Ergebnisse (Brave)" if lang == "de" else "## 🌐 Web results (Brave)"
    lines = [header, ""]
    for h in hits[:5]:
        title = (h.get("title") or "").strip().replace("\n", " ")
        url = (h.get("url") or "").strip()
        desc = (h.get("description") or "").strip().replace("\n", " ")
        if len(desc) > 240:
            desc = desc[:240] + "…"
        lines.append(f"- **{title}** — {url}\n  {desc}")
    return "\n".join(lines)


async def gather_external_context(
    text: str,
    *,
    lang: str = "de",
    max_libraries: int = 2,
    docs_tokens_per_lib: int = 2500,
    enabled_context7: bool = True,
    enabled_websearch: bool = True,
) -> str | None:
    """Best-effort: detect libs / web-needs in `text`, return a single
    markdown block or None. Concurrent fetches; total budget ~10s."""
    if not text or not text.strip():
        return None

    libraries = detect_libraries(text, max_hits=max_libraries) if enabled_context7 else []
    web_query: str | None = None
    if enabled_websearch and brave_is_configured() and needs_web_search(text, lang):
        # Use the first 200 chars of the task as the search query — keeps
        # it focused without hitting Brave's query length limit.
        web_query = text.strip()[:200]

    if not libraries and not web_query:
        return None

    coros: list = []
    for lib in libraries:
        coros.append(docs_for_query(lib, tokens=docs_tokens_per_lib))
    if web_query:
        coros.append(brave_search(web_query, count=5, lang=lang))

    results = await asyncio.gather(*coros, return_exceptions=True)
    docs_blocks: list[str] = []
    web_hits: list[dict] = []
    for i, res in enumerate(results):
        if isinstance(res, Exception):
            log.debug("research task %d failed: %s", i, res)
            continue
        if i < len(libraries):
            if isinstance(res, str) and res.strip():
                docs_blocks.append(res)
        else:
            if isinstance(res, list):
                web_hits = res

    sections: list[str] = []
    if docs_blocks:
        header = "## 📚 Library-Docs (Context7)" if lang == "de" else "## 📚 Library docs (Context7)"
        sections.append(header + "\n\n" + "\n\n---\n\n".join(docs_blocks))
    web_block = _format_web_hits(web_hits, lang)
    if web_block:
        sections.append(web_block)

    if not sections:
        return None
    intro = (
        "=== EXTERNAL CONTEXT (auto-fetched) ==="
        if lang == "en"
        else "=== EXTERNES KONTEXT-MATERIAL (automatisch geladen) ==="
    )
    body = "\n\n".join(sections)
    # Hard cap so the prompt doesn't blow up on weirdly-large doc sets.
    if len(body) > 18_000:
        body = body[:18_000] + "\n\n…[truncated]"
    return f"{intro}\n\n{body}"
