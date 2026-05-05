"""Plan v5 R6 — SONA-Lernmuster: erfolgreiche Run-Trajektorien speichern + bei
ähnlichen Tasks vorschlagen.

Inspiration: Ruflo's intelligence-Plugin (SONA). Bei jedem erfolgreichen
Run wird das Pattern (Task → Plan → Sub-Task-Sequenz → Files-Changed →
Outcome) gespeichert. Bei einem neuen Task: top-K ähnliche Patterns
liefern dem Planner Kontext „so was hat schon geklappt".

Storage:
  - JSONL unter <CASCADE_HOME>/store/patterns.jsonl
  - Optional auto-indexed in cascade.rag (wenn deps verfügbar) als
    eigene Source „pattern" mit hohem RRF-Weight

Lookup:
  - find_similar(task_text, top_n=3) — Keyword-Heuristik (default) ODER
    via RAG-Search (wenn enabled)
  - Bei mehreren Matches: ranked by recency + success-Score

Integration:
  - cascade.reflect.persist_lessons() ruft pattern.record_pattern() auf
    erfolgreiche Runs (status=done UND iter > 1, weil 1-iter-Runs zu
    trivial sind um als Lehrbeispiel zu zählen)
  - Planner-Prompt erweitert um optional „PRIOR_SUCCESSFUL_PATTERNS"-Block
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


logger = logging.getLogger("cascade.patterns")


@dataclass
class TaskPattern:
    """Eine gespeicherte erfolgreiche Run-Trajektorie."""
    pattern_id: str            # hash aus task + timestamp
    task_text: str             # Original-Task
    task_keywords: List[str] = field(default_factory=list)
    plan_summary: str = ""
    sub_task_names: List[str] = field(default_factory=list)
    files_changed: List[str] = field(default_factory=list)
    iterations: int = 0
    cost_usd: float = 0.0
    wall_clock_s: float = 0.0
    saved_at: float = field(default_factory=time.time)
    # Quality-Score: 1.0 wenn passed im 1. iter, 0.5 wenn replan nötig
    quality_score: float = 1.0


# ──────────────────────────────────────────────────────────────────────
#  Keyword-Extraktion
# ──────────────────────────────────────────────────────────────────────
_TOKENIZE_RX = re.compile(r"[A-Za-zÄÖÜäöü0-9_]+")
_STOPWORDS_DE = {
    "der", "die", "das", "den", "dem", "des",
    "und", "oder", "aber", "nicht", "kein",
    "für", "bei", "von", "mit", "als", "auf", "in", "an", "zu",
    "ist", "sind", "war", "waren", "wird", "wurde",
    "ein", "eine", "einer", "einen", "eines",
    "ich", "du", "er", "sie", "wir", "ihr",
    "du", "dass", "damit", "weil",
    "im", "am", "vom", "zum", "zur",
}
_STOPWORDS_EN = {
    "the", "and", "or", "but", "not", "is", "are", "was", "were", "be",
    "a", "an", "of", "to", "in", "on", "at", "for", "with", "from", "by",
    "this", "that", "these", "those", "it", "its",
    "i", "you", "he", "she", "we", "they",
    "do", "does", "did", "have", "has", "had",
    "if", "else", "then",
}
_STOPWORDS = _STOPWORDS_DE | _STOPWORDS_EN


def extract_keywords(text: str, *, max_n: int = 12) -> List[str]:
    """Einfache Keyword-Extraktion: tokens, lowercase, no-stopwords,
    sorted by length DESC, top-N."""
    if not text:
        return []
    tokens = _TOKENIZE_RX.findall(text.lower())
    seen = set()
    keep: List[str] = []
    for t in tokens:
        if len(t) < 3:
            continue
        if t in _STOPWORDS:
            continue
        if t in seen:
            continue
        seen.add(t)
        keep.append(t)
    # Sortiere nach Länge desc — längere Wörter sind oft spezifischer
    keep.sort(key=lambda w: (-len(w), w))
    return keep[:max_n]


# ──────────────────────────────────────────────────────────────────────
#  Pattern-Storage (JSONL append-only)
# ──────────────────────────────────────────────────────────────────────
class PatternStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, pattern: TaskPattern) -> None:
        line = json.dumps(asdict(pattern), ensure_ascii=False, default=str)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        logger.info(f"pattern saved: {pattern.pattern_id} ({len(pattern.task_keywords)} kw)")

    def all(self) -> List[TaskPattern]:
        if not self.path.exists():
            return []
        out: List[TaskPattern] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                out.append(TaskPattern(**d))
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"skip malformed pattern: {e}")
        return out


# ──────────────────────────────────────────────────────────────────────
#  Recording
# ──────────────────────────────────────────────────────────────────────
def make_pattern_id(task_text: str, ts: float) -> str:
    h = hashlib.sha1(f"{task_text}|{ts}".encode("utf-8")).hexdigest()[:12]
    return f"pat_{h}"


def record_pattern(
    *,
    store: PatternStore,
    task_text: str,
    plan_summary: str = "",
    sub_task_names: Optional[List[str]] = None,
    files_changed: Optional[List[str]] = None,
    iterations: int = 1,
    cost_usd: float = 0.0,
    wall_clock_s: float = 0.0,
    replans_needed: int = 0,
) -> TaskPattern:
    """Ein erfolgreich abgeschlossener Run wird als wiederverwendbares
    Pattern gespeichert.

    quality_score wird heuristisch aus replans_needed abgeleitet:
      0 replans → 1.0
      1 replan  → 0.7
      2+ replans → 0.4
    """
    if replans_needed <= 0:
        quality = 1.0
    elif replans_needed == 1:
        quality = 0.7
    else:
        quality = 0.4

    pattern = TaskPattern(
        pattern_id=make_pattern_id(task_text, time.time()),
        task_text=task_text[:2000],
        task_keywords=extract_keywords(task_text),
        plan_summary=plan_summary[:500],
        sub_task_names=list(sub_task_names or []),
        files_changed=list(files_changed or [])[:50],
        iterations=iterations,
        cost_usd=cost_usd,
        wall_clock_s=wall_clock_s,
        quality_score=quality,
    )
    store.append(pattern)
    return pattern


# ──────────────────────────────────────────────────────────────────────
#  Lookup
# ──────────────────────────────────────────────────────────────────────
def jaccard_similarity(a: List[str], b: List[str]) -> float:
    """Jaccard-Coefficient auf Keyword-Sets."""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def find_similar(
    *,
    store: PatternStore,
    task_text: str,
    top_n: int = 3,
    min_similarity: float = 0.2,
) -> List[Dict[str, Any]]:
    """Findet die top-N ähnlichsten Patterns via Keyword-Jaccard.

    Returns Liste von {pattern, similarity, score}-dicts. score
    kombiniert similarity + recency + quality.
    """
    query_kw = extract_keywords(task_text)
    if not query_kw:
        return []
    patterns = store.all()
    if not patterns:
        return []

    now = time.time()
    scored: List[Dict[str, Any]] = []
    for p in patterns:
        sim = jaccard_similarity(query_kw, p.task_keywords)
        if sim < min_similarity:
            continue
        # Recency: 1.0 für heute, 0.5 für 30 Tage alt, 0 für >365 Tage
        age_days = (now - p.saved_at) / 86400.0
        recency = max(0.0, 1.0 - age_days / 365.0)
        # Combined: sim*0.6 + recency*0.2 + quality*0.2
        combined = sim * 0.6 + recency * 0.2 + p.quality_score * 0.2
        scored.append({
            "pattern": p,
            "similarity": sim,
            "recency": recency,
            "quality": p.quality_score,
            "score": combined,
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_n]


def render_for_planner(
    similar: List[Dict[str, Any]],
    *,
    lang: str = "de",
) -> str:
    """Rendert similarity-results als Planner-Prompt-Block."""
    if not similar:
        return ""
    if lang == "de":
        head = "# PRIOR_SUCCESSFUL_PATTERNS\n"
        head += (
            "Bei ähnlichen Aufgaben hat sich folgendes bewährt — kannst du als "
            "Hinweis nutzen, must aber nicht stur kopieren:\n\n"
        )
    else:
        head = "# PRIOR_SUCCESSFUL_PATTERNS\n"
        head += (
            "For similar tasks, the following approach worked. Use as hint, "
            "don't copy blindly:\n\n"
        )
    blocks: List[str] = []
    for i, item in enumerate(similar, 1):
        p: TaskPattern = item["pattern"]
        files_str = ", ".join(p.files_changed[:5]) or "—"
        sub_str = " → ".join(p.sub_task_names) if p.sub_task_names else "(no decomposition)"
        score = item["score"]
        blocks.append(
            f"## Pattern {i} (score={score:.2f}, sim={item['similarity']:.2f})\n"
            f"  Task (excerpt): {p.task_text[:200]}…\n"
            f"  Plan: {p.plan_summary[:200]}\n"
            f"  Sub-Tasks: {sub_str}\n"
            f"  Files touched: {files_str}\n"
            f"  Iterations: {p.iterations}, Cost: ${p.cost_usd:.4f}, Quality: {p.quality_score:.1f}"
        )
    return head + "\n\n".join(blocks)
