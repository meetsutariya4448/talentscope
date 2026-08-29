"""
Phase 2 tests: RRF logic (pure), API mode param, hybrid vs FTS integration.
"""
import numpy as np
import pytest

from app.search.hybrid import reciprocal_rank_fusion
from app.models import Company, Posting


# ---------------------------------------------------------------------------
# Pure unit tests — no DB, no model
# ---------------------------------------------------------------------------

def test_rrf_item_in_both_lists_scores_higher():
    """An ID in both lists should outscore an ID in only one."""
    fts  = [1, 2, 3]
    vec  = [2, 1, 4]
    result = reciprocal_rank_fusion([fts, vec])
    # 1 and 2 appear in both; 3 and 4 in only one
    assert result.index(1) < result.index(3)
    assert result.index(2) < result.index(4)


def test_rrf_all_ids_present():
    """Every input ID from every list must appear in the output."""
    result = reciprocal_rank_fusion([[10, 20], [30, 40]])
    assert set(result) == {10, 20, 30, 40}


def test_rrf_single_list_preserves_order():
    """With one list, output order matches input order."""
    ids = [5, 3, 1]
    assert reciprocal_rank_fusion([ids]) == ids


def test_rrf_empty_lists():
    assert reciprocal_rank_fusion([[], []]) == []
    assert reciprocal_rank_fusion([]) == []


def test_rrf_k_parameter():
    """Higher k compresses score differences but doesn't change set membership."""
    a, b = [1, 2], [2, 3]
    r_low  = reciprocal_rank_fusion([a, b], k=1)
    r_high = reciprocal_rank_fusion([a, b], k=10_000)
    assert set(r_low) == set(r_high) == {1, 2, 3}


def test_rrf_top_item_consistent():
    """ID ranked #1 in both lists should be the overall top result."""
    winner = 99
    result = reciprocal_rank_fusion([[winner, 1, 2], [winner, 3, 4]])
    assert result[0] == winner


def test_rrf_duplicate_within_source_does_not_inflate_score():
    """One retrieval source cannot vote repeatedly for the same posting."""
    result = reciprocal_rank_fusion([[1, 1, 2], [2]])

    assert result == [2, 1]


# ---------------------------------------------------------------------------
# API mode parameter tests — purely structural, no real search results needed
# ---------------------------------------------------------------------------

def test_fts_mode_default(client):
    resp = client.get("/postings/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "fts"
    assert "results" in data


def test_fts_mode_explicit(client):
    resp = client.get("/postings/?mode=fts")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "fts"


def test_vector_mode_accepted(client):
    resp = client.get("/postings/?mode=vector&q=python+engineer")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "vector"
    assert "results" in data


def test_hybrid_mode_accepted(client):
    resp = client.get("/postings/?mode=hybrid&q=backend+engineer")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] in ("hybrid", "fts")  # falls back to fts when no q
    assert "results" in data


def test_invalid_mode_rejected(client):
    resp = client.get("/postings/?mode=magic")
    assert resp.status_code == 422


def test_vector_mode_no_query_falls_back(client):
    """mode=vector with no q should fall back to FTS (no query to encode)."""
    resp = client.get("/postings/?mode=vector")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "fts"


def test_vector_mode_whitespace_query_falls_back(client, monkeypatch):
    """Whitespace-only input has no semantic content and must not be encoded."""
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("vector search must not run for a blank query")

    monkeypatch.setattr("app.api.postings.vector_search", fail_if_called)

    resp = client.get("/postings/?mode=vector&q=%20%20%20")

    assert resp.status_code == 200
    assert resp.json()["mode"] == "fts"


# ---------------------------------------------------------------------------
# Integration: hybrid returns same relevant postings as FTS, plus any extras
# from vector recall
# ---------------------------------------------------------------------------

def test_hybrid_recall_not_worse_than_fts(client, db, monkeypatch):
    """
    For a keyword that has a clear FTS match, hybrid must return at least the
    same result.  Vector component uses a deterministic fake encoder so the
    test never downloads the model.
    """
    # Seed data
    co = Company(name="HybridTestCo", slug="hybridtestco-search")
    db.add(co)
    db.flush()

    p = Posting(
        company_id=co.id,
        title="Senior Python Engineer",
        description="We need Python, FastAPI, and PostgreSQL skills.",
        location="Remote",
        source="greenhouse",
        source_id="hybrid-test-001",
        currency="USD",
    )
    db.add(p)
    db.flush()  # search_vector is a GENERATED column — auto-populates on flush

    # Give it a unit vector so vector_search can find it
    vec = np.zeros(384)
    vec[0] = 1.0
    p.embedding = vec.tolist()
    db.commit()

    # Fake encoder: always returns a vector pointing in the same direction
    class _FakeModel:
        def encode(self, _text, normalize_embeddings=True):
            v = np.zeros(384)
            v[0] = 1.0
            return v

    monkeypatch.setattr("app.search.encoder._model", _FakeModel())

    fts_resp    = client.get("/postings/?mode=fts&q=python+engineer")
    hybrid_resp = client.get("/postings/?mode=hybrid&q=python+engineer")

    assert fts_resp.status_code == 200
    assert hybrid_resp.status_code == 200

    fts_ids    = {r["id"] for r in fts_resp.json()["results"]}
    hybrid_ids = {r["id"] for r in hybrid_resp.json()["results"]}

    # Seeded posting must appear in both
    assert p.id in fts_ids,    "FTS should find the seeded posting"
    assert p.id in hybrid_ids, "Hybrid must include everything FTS finds"

    # Hybrid recall must be >= FTS recall
    assert fts_ids.issubset(hybrid_ids), (
        f"Hybrid missed IDs that FTS found: {fts_ids - hybrid_ids}"
    )
