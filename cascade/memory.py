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

PROJECT = "cascade-bot-mcp"


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
    tags: str = "cascade-bot-mcp",
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


# Minimal stopword list (DE + EN). Kept small on purpose — the BM25 IDF
# already discounts common terms, but trimming the obvious ones first
# keeps the tokenized query focused and avoids matching "for"/"the"/etc.
_STOPWORDS: frozenset[str] = frozenset({
    # English
    "the", "and", "for", "with", "from", "this", "that", "have", "has",
    "are", "was", "were", "you", "your", "our", "out", "into", "but",
    "not", "all", "any", "can", "will", "would", "could", "should", "may",
    "might", "what", "which", "who", "how", "why", "when", "where",
    "there", "here", "than", "then", "them", "they", "their", "ours",
    # German
    "und", "oder", "aber", "der", "die", "das", "den", "dem", "des",
    "ein", "eine", "einen", "einem", "einer", "ist", "war", "sind", "waren",
    "ich", "du", "er", "sie", "es", "wir", "ihr", "mit", "von", "zu",
    "bei", "auf", "für", "über", "unter", "nach", "vor", "ohne", "gegen",
    "wie", "wann", "warum", "wo", "was", "wer", "ja", "nein", "nicht",
    "doch", "mal", "schon", "noch", "auch", "nur", "sehr", "dann",
})


def _tokenize(text: str, *, min_len: int = 3) -> list[str]:
    """Lowercase, split on non-alnum, filter stopwords + min length.
    Returns a list of tokens (preserving multiplicity for term-frequency)."""
    if not text:
        return []
    out: list[str] = []
    cur: list[str] = []
    for ch in text.lower():
        if ch.isalnum():
            cur.append(ch)
        else:
            if cur:
                tok = "".join(cur)
                cur = []
                if len(tok) >= min_len and tok not in _STOPWORDS:
                    out.append(tok)
    if cur:
        tok = "".join(cur)
        if len(tok) >= min_len and tok not in _STOPWORDS:
            out.append(tok)
    return out


_IMPORTANCE_BOOST = {
    "critical": 1.30,
    "high":     1.15,
    "medium":   1.00,
    "low":      0.85,
}


def _bm25_score(
    query_terms: list[str],
    doc_terms: list[str],
    df: dict[str, int],
    n_docs: int,
    avgdl: float,
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> float:
    """Vanilla BM25 score for one (query, doc). df = document-frequency map
    over the whole collection. Returns 0.0 if no query term hits."""
    if not query_terms or not doc_terms:
        return 0.0
    dl = len(doc_terms)
    # Term-frequency map for this doc
    tf: dict[str, int] = {}
    for t in doc_terms:
        tf[t] = tf.get(t, 0) + 1
    score = 0.0
    import math
    for q in set(query_terms):
        f = tf.get(q, 0)
        if f == 0:
            continue
        n_q = df.get(q, 0)
        idf = math.log(1.0 + (n_docs - n_q + 0.5) / (n_q + 0.5))
        norm = f * (k1 + 1.0) / (f + k1 * (1.0 - b + b * (dl / max(avgdl, 1.0))))
        score += idf * norm
    return score


async def recall_context(task: str, *, limit: int = 3) -> str | None:
    """BM25-ranked recall over the local memory.jsonl. Searches across both
    `content` and `file_content`-like fields plus tags. Importance metadata
    nudges the ranking (`high`/`critical` rank slightly higher).

    Returns a bullet-list of the top `limit` matches, or None if nothing
    scores above zero.
    """
    path = _memory_path()
    if not path.exists():
        return None

    def _scan_and_rank() -> list[tuple[float, dict]]:
        q_terms = _tokenize(task)
        if not q_terms:
            return []

        # First pass — load entries + tokenize. This is bounded by the
        # JSONL size; we cap at the latest 5000 entries to keep recall
        # snappy even when the file has grown over months of use.
        entries: list[dict] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            return []
        if len(entries) > 5000:
            entries = entries[-5000:]
        if not entries:
            return []

        # Build per-doc token lists + document-frequency map
        docs: list[list[str]] = []
        df: dict[str, int] = {}
        for e in entries:
            haystack = " ".join(
                str(e.get(k, ""))
                for k in ("content", "tags", "category")
            )
            tokens = _tokenize(haystack)
            docs.append(tokens)
            for t in set(tokens):
                df[t] = df.get(t, 0) + 1
        n_docs = len(docs)
        avgdl = sum(len(d) for d in docs) / max(n_docs, 1)

        # Score every doc; keep only those with score>0
        scored: list[tuple[float, dict]] = []
        for e, dt in zip(entries, docs):
            s = _bm25_score(q_terms, dt, df, n_docs, avgdl)
            if s <= 0.0:
                continue
            boost = _IMPORTANCE_BOOST.get(e.get("importance", "medium"), 1.0)
            scored.append((s * boost, e))

        scored.sort(key=lambda p: p[0], reverse=True)
        return scored[:limit]

    ranked = await asyncio.to_thread(_scan_and_rank)
    if not ranked:
        return None
    lines = []
    for score, e in ranked:
        cat = e.get("category", "?")
        imp = e.get("importance", "?")
        content = (e.get("content") or "")[:240]
        lines.append(f"  [{cat}/{imp} score={score:.2f}] {content}")
    return "\n".join(lines)
