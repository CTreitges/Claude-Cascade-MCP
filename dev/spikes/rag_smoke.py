"""Plan v5 R4 — RAG-Memory Smoke (ohne harte deps)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from cascade.rag import (
    RagDoc,
    RagHit,
    RagStore,
    is_available,
    reciprocal_rank_fusion,
)


def passed(label):
    print(f"  ✅ {label}")


def fail(label, why):
    print(f"  ❌ {label}: {why}")
    raise SystemExit(1)


def test_is_available():
    print("\n[1] is_available — gracefully reports missing deps")
    avail = is_available()
    print(f"     chromadb+sentence-transformers verfügbar: {avail}")
    # Dieser Test scheitert NICHT — er reportet nur den State
    passed(f"reported state: {avail}")


def test_store_no_op_when_disabled():
    print("\n[2] RagStore: no-op wenn deps fehlen oder enabled=False")
    store = RagStore(persist_dir="/tmp/cascade-rag-test", enabled=False)
    docs = [RagDoc(id="a1", text="hello world", source="rlm")]
    n = store.upsert(docs)
    assert n == 0, "no-op sollte 0 returnen"
    hits = store.search("hello")
    assert hits == []
    passed("disabled store returnt 0/[] für upsert/search")


def test_reciprocal_rank_fusion_empty():
    print("\n[3] RRF mit leeren Listen")
    res = reciprocal_rank_fusion([], [])
    assert res == []
    passed("RRF([], []) = []")


def test_reciprocal_rank_fusion_rlm_only():
    print("\n[4] RRF mit nur RLM-Hits")
    rlm = [
        {"id": "r1", "content": "first hit"},
        {"id": "r2", "content": "second hit"},
    ]
    res = reciprocal_rank_fusion(rlm, [])
    assert len(res) == 2
    assert res[0]["id"] == "r1"
    assert res[0]["source"] == "rlm"
    assert res[0]["rrf_score"] > res[1]["rrf_score"]
    passed(f"r1 vor r2: scores {[round(r['rrf_score'], 4) for r in res]}")


def test_reciprocal_rank_fusion_combined():
    print("\n[5] RRF kombiniert + Dedupe bei gleicher ID")
    rlm = [
        {"id": "shared", "content": "from rlm"},
        {"id": "rlm-only", "content": "only rlm"},
    ]
    rag = [
        RagHit(doc_id="shared", text="from rag", score=0.9, source="rag"),
        RagHit(doc_id="rag-only", text="only rag", score=0.7, source="rag"),
    ]
    res = reciprocal_rank_fusion(rlm, rag)
    assert len(res) == 3, f"expected 3 unique, got {len(res)}"
    # 'shared' sollte am höchsten scoren weil in beiden
    top = res[0]
    assert top["id"] == "shared"
    print(f"     fused order: {[(r['id'], round(r['rrf_score'], 4)) for r in res]}")
    passed("dedup + shared id boosted")


def test_rrf_weighted():
    print("\n[6] RRF Gewichtung: RLM-Weight höher")
    rlm = [{"id": "rlm-1", "content": "x"}]
    rag = [RagHit(doc_id="rag-1", text="y", score=0.9, source="rag")]
    res = reciprocal_rank_fusion(rlm, rag, rlm_weight=1.5, rag_weight=1.0)
    assert res[0]["id"] == "rlm-1", "RLM sollte gewinnen wegen weight"
    passed("RLM weight 1.5 > RAG weight 1.0")


def test_persist_dir_creation():
    print("\n[7] persist_dir wird erstellt (auch wenn deps fehlen)")
    import tempfile, os
    tmp = Path(tempfile.gettempdir()) / "cascade-rag-spike-init"
    if tmp.exists():
        import shutil
        shutil.rmtree(tmp)
    store = RagStore(persist_dir=tmp, enabled=False)
    # Bei enabled=False wird _ensure_client nicht ausgeführt
    # Aber persist_dir wird in __init__ resolved
    assert str(tmp) in store.persist_dir or store.persist_dir == str(tmp.resolve())
    passed(f"persist_dir resolved: {store.persist_dir}")


def main():
    print("=" * 60)
    print("  Plan v5 R4 — RAG-Memory Smoke")
    print("=" * 60)
    test_is_available()
    test_store_no_op_when_disabled()
    test_reciprocal_rank_fusion_empty()
    test_reciprocal_rank_fusion_rlm_only()
    test_reciprocal_rank_fusion_combined()
    test_rrf_weighted()
    test_persist_dir_creation()
    print("\n" + "=" * 60)
    print("  ✅ Alle 7 Tests grün (RAG-Layer optional)")
    print("=" * 60)


if __name__ == "__main__":
    main()
