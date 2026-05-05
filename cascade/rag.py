"""Plan v5 R4 — RAG-Memory neben RLM.

Inspiration: Ruflo's rag-memory + agentdb. RLM bleibt PRIMARY (BM25,
manuell-kuratiert). RAG ist SECONDARY (semantic, cross-project,
auto-indexed). Two-Tier-Lookup mit reciprocal-rank-fusion.

Architektur:
    recall_context(query):
      ├── RLM-Lookup (BM25, exakt, project-scoped)   ← primary
      └── RAG-Lookup (HNSW Vector, semantic, broad)  ← secondary
        → reciprocal-rank-fusion → top-N

Optional dependencies:
  - chromadb (vector store)
  - sentence-transformers (embeddings, lokal)

Wenn nicht installiert: RAG-Layer ist no-op (recall_combined fällt
auf reine RLM-Suche zurück). Install: pip install chromadb sentence-transformers

Index-Quellen:
  - RLM-Insights (jeder rlm_remember triggert auto-embed)
  - Erfolgreich abgeschlossene Sub-Tasks (auto-indexed bei status=done)
  - Pattern-Library (Phase R6)

RLM bleibt SoT — RAG ist derived/cache. Bei chroma-corruption: drop +
re-index aus RLM.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger("cascade.rag")


@dataclass
class RagDoc:
    """Ein Dokument im RAG-Index."""
    id: str                       # eindeutige ID, z.B. "rlm:abc123" oder "task:abc"
    text: str                     # was gesucht wird (volltext oder summary)
    metadata: Dict[str, Any] = field(default_factory=dict)
    source: str = "unknown"       # "rlm" / "task" / "pattern"


@dataclass
class RagHit:
    doc_id: str
    text: str
    score: float                  # höher = besser
    source: str
    metadata: Dict[str, Any] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────
#  Optional dependency-Check
# ──────────────────────────────────────────────────────────────────────
def _has_chroma() -> bool:
    try:
        import chromadb  # noqa: F401
        return True
    except ImportError:
        return False


def _has_st() -> bool:
    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


def is_available() -> bool:
    """True wenn alle deps installiert + lazy-loadable."""
    return _has_chroma() and _has_st()


# ──────────────────────────────────────────────────────────────────────
#  RagStore
# ──────────────────────────────────────────────────────────────────────
class RagStore:
    """Embeddet + indexiert Dokumente. No-op wenn deps fehlen.

    Multi-Collection: jede source (rlm / task / pattern) eigene Collection
    für getrennte Re-Indexing-Cycles und gewichtetes Hybrid-Search.
    """

    def __init__(
        self,
        *,
        persist_dir: str | Path,
        embedding_model: str = "sentence-transformers/multi-qa-MiniLM-L6-cos-v1",
        collection_prefix: str = "cascade",
        enabled: Optional[bool] = None,
    ) -> None:
        self.persist_dir = str(Path(persist_dir).resolve())
        self.embedding_model = embedding_model
        self.collection_prefix = collection_prefix
        self.enabled = enabled if enabled is not None else is_available()
        self._client = None
        self._embedder = None
        self._collections: Dict[str, Any] = {}

    def _ensure_client(self) -> bool:
        """Lazy client-init. Returns True wenn ready."""
        if not self.enabled:
            return False
        if self._client is not None:
            return True
        try:
            import chromadb
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            logger.warning(
                f"RagStore: optional deps nicht installiert ({e}). "
                f"Install via: pip install chromadb sentence-transformers"
            )
            self.enabled = False
            return False

        os.makedirs(self.persist_dir, exist_ok=True)
        self._client = chromadb.PersistentClient(path=self.persist_dir)
        self._embedder = SentenceTransformer(self.embedding_model)
        logger.info(
            f"RagStore: chroma persistent at {self.persist_dir}, "
            f"embedder={self.embedding_model}"
        )
        return True

    def _collection(self, source: str):
        if not self._ensure_client():
            return None
        name = f"{self.collection_prefix}_{source}"
        if name not in self._collections:
            self._collections[name] = self._client.get_or_create_collection(name=name)
        return self._collections[name]

    def _embed(self, texts: List[str]) -> List[List[float]]:
        if not self._ensure_client():
            return []
        return self._embedder.encode(texts, convert_to_numpy=False).tolist()

    # ── Index-Operations ──────────────────────────────────────────────
    def upsert(self, docs: List[RagDoc]) -> int:
        """Idempotent insert/update. Returns Anzahl aktualisierter Docs."""
        if not docs or not self._ensure_client():
            return 0
        # Group nach source
        by_source: Dict[str, List[RagDoc]] = {}
        for d in docs:
            by_source.setdefault(d.source, []).append(d)

        total = 0
        for source, group in by_source.items():
            coll = self._collection(source)
            if coll is None:
                continue
            embeddings = self._embed([d.text for d in group])
            coll.upsert(
                ids=[d.id for d in group],
                embeddings=embeddings,
                documents=[d.text for d in group],
                metadatas=[d.metadata for d in group],
            )
            total += len(group)
        return total

    def remove(self, doc_ids: List[str], source: str) -> int:
        if not doc_ids or not self._ensure_client():
            return 0
        coll = self._collection(source)
        if coll is None:
            return 0
        coll.delete(ids=doc_ids)
        return len(doc_ids)

    def count(self, source: Optional[str] = None) -> int:
        if not self._ensure_client():
            return 0
        if source:
            coll = self._collection(source)
            return coll.count() if coll else 0
        # alle sources summieren
        total = 0
        # chromadb hat kein direct list_collections im persistent client API,
        # aber wir tracken self._collections selbst
        for c in self._collections.values():
            try:
                total += c.count()
            except Exception:
                pass
        return total

    # ── Search ────────────────────────────────────────────────────────
    def search(
        self,
        query: str,
        *,
        source: Optional[str] = None,
        n: int = 5,
    ) -> List[RagHit]:
        """Semantic-Search. source=None → alle bekannten sources, dann fusion.

        Returns top-N RagHits sorted by score desc.
        """
        if not self._ensure_client() or not query:
            return []
        emb = self._embed([query])
        if not emb:
            return []
        query_vec = emb[0]

        sources = [source] if source else list({
            n.replace(f"{self.collection_prefix}_", "")
            for n in self._collections.keys()
        }) or ["rlm", "task", "pattern"]

        all_hits: List[RagHit] = []
        for s in sources:
            coll = self._collection(s)
            if coll is None or coll.count() == 0:
                continue
            try:
                res = coll.query(
                    query_embeddings=[query_vec],
                    n_results=min(n, coll.count()),
                )
            except Exception as e:
                logger.warning(f"chroma query failed for source={s}: {e}")
                continue
            ids = (res.get("ids") or [[]])[0]
            docs = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            distances = (res.get("distances") or [[]])[0]
            for i, doc_id in enumerate(ids):
                # cosine distance → similarity (1 - distance, clamped to ≥0)
                dist = distances[i] if i < len(distances) else 1.0
                sim = max(0.0, 1.0 - float(dist))
                all_hits.append(RagHit(
                    doc_id=doc_id,
                    text=docs[i] if i < len(docs) else "",
                    score=sim,
                    source=s,
                    metadata=metas[i] if i < len(metas) else {},
                ))

        all_hits.sort(key=lambda h: h.score, reverse=True)
        return all_hits[:n]


# ──────────────────────────────────────────────────────────────────────
#  Hybrid Recall: RLM (BM25) + RAG (Vector) → reciprocal-rank-fusion
# ──────────────────────────────────────────────────────────────────────
def reciprocal_rank_fusion(
    rlm_hits: List[Dict[str, Any]],
    rag_hits: List[RagHit],
    *,
    rrf_k: int = 60,
    rlm_weight: float = 1.5,    # RLM ist primary, höheres Gewicht
    rag_weight: float = 1.0,
) -> List[Dict[str, Any]]:
    """Verbindet zwei ranked-Listen über reciprocal-rank-fusion.

    RRF-Score = sum(weight / (rrf_k + rank))

    Args:
        rlm_hits: Liste von dicts mit mind. 'id' + 'content' Feldern
        rag_hits: Liste von RagHit-Objekten
        rrf_k: Diskontierungsfaktor (typisch 60)
        rlm_weight / rag_weight: Quellen-Gewichtung

    Returns: dedupli­zierte Hit-Liste mit kombiniertem 'rrf_score'.
    """
    scores: Dict[str, float] = {}
    items: Dict[str, Dict[str, Any]] = {}

    for rank, hit in enumerate(rlm_hits or [], start=1):
        hid = str(hit.get("id") or hit.get("chunk_id") or hit.get("rlm_id") or rank)
        score = rlm_weight / (rrf_k + rank)
        scores[hid] = scores.get(hid, 0.0) + score
        if hid not in items:
            items[hid] = {
                "id": hid,
                "content": hit.get("content") or hit.get("text") or str(hit),
                "source": "rlm",
                "metadata": hit,
            }

    for rank, hit in enumerate(rag_hits or [], start=1):
        score = rag_weight / (rrf_k + rank)
        # bei doppelten ids (selbe Insight in RLM + RAG): Score addiert sich
        scores[hit.doc_id] = scores.get(hit.doc_id, 0.0) + score
        if hit.doc_id not in items:
            items[hit.doc_id] = {
                "id": hit.doc_id,
                "content": hit.text,
                "source": hit.source if hit.source else "rag",
                "metadata": hit.metadata,
            }

    fused = []
    for hid, score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
        item = dict(items[hid])
        item["rrf_score"] = score
        fused.append(item)
    return fused
