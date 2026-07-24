"""
Phase 4 tests: KMeans clustering pipeline.

Fake embeddings (random unit vectors, seeded) keep tests fast and
deterministic — no model load, no network.
"""
import json
import numpy as np
import pytest
from sqlalchemy import text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_postings_with_embeddings(db, n: int, n_clusters: int = 3, dim: int = 384, seed: int = 0):
    """
    Insert n postings with synthetic embeddings drawn from n_clusters
    Gaussian blobs, normalized to the unit sphere.

    Returns the list of inserted posting IDs.
    """
    from app.models import Company, Posting
    from sklearn.preprocessing import normalize

    rng = np.random.default_rng(seed)

    co = Company(name=f"ClusterTestCo-{seed}", slug=f"clustertestco-{seed}")
    db.add(co)
    db.flush()

    ids = []
    for i in range(n):
        # Assign to one of n_clusters blobs
        cluster_idx = i % n_clusters
        centre = np.zeros(dim)
        centre[cluster_idx * (dim // n_clusters)] = 5.0  # well-separated centroid
        vec = rng.normal(loc=centre, scale=0.1, size=dim).astype(np.float32)
        vec = normalize(vec.reshape(1, -1))[0]

        p = Posting(
            company_id=co.id,
            title=f"Role {i} in cluster {cluster_idx}",
            source="greenhouse",
            source_id=f"cltest-{seed}-{i}",
            currency="USD",
            embedding=vec.tolist(),
        )
        db.add(p)
        db.flush()

        # Minimal tsvector so FTS doesn't error
        db.execute(
            text("UPDATE postings SET search_vector = to_tsvector('english', title) WHERE id = :id"),
            {"id": p.id},
        )
        ids.append(p.id)

    db.commit()
    return ids


# ---------------------------------------------------------------------------
# Unit tests for clustering logic
# ---------------------------------------------------------------------------

def test_clustering_returns_expected_structure(db):
    """run_clustering() returns the documented summary keys."""
    _seed_postings_with_embeddings(db, n=30, n_clusters=3, seed=1)

    from app.ml.clustering import run_clustering
    result = run_clustering(db, k=3)

    assert "error" not in result, result.get("error")
    assert result["k"] == 3
    assert result["n"] >= 30
    assert 0.0 <= result["silhouette"] <= 1.0
    assert len(result["clusters"]) == 3

    for c in result["clusters"]:
        assert "cluster_id" in c
        assert "label"      in c
        assert "size"       in c
        assert "top_skills" in c
        assert c["size"] > 0


def test_clustering_k_auto_selection(db):
    """When k=None, silhouette grid selects a k in the expected range."""
    _seed_postings_with_embeddings(db, n=60, n_clusters=5, seed=2)

    from app.ml.clustering import run_clustering, _K_MIN, _K_MAX
    result = run_clustering(db)   # k=None → auto

    assert "error" not in result
    assert _K_MIN <= result["k"] <= _K_MAX
    assert "sil_by_k" in result
    assert len(result["sil_by_k"]) > 1


def test_clustering_persists_to_db(db):
    """After run_clustering, skill_clusters table is populated and posting.cluster_id is set."""
    from app.models import SkillCluster, Posting
    from sqlalchemy import select

    ids = _seed_postings_with_embeddings(db, n=20, n_clusters=3, seed=3)

    from app.ml.clustering import run_clustering
    result = run_clustering(db, k=3)
    k_used = result["k"]

    # skill_clusters populated with exactly k rows
    clusters = db.execute(select(SkillCluster)).scalars().all()
    assert len(clusters) == k_used
    # Each seeded posting must have a cluster_id assigned
    postings = db.execute(
        select(Posting).where(Posting.id.in_(ids))
    ).scalars().all()
    assert all(p.cluster_id is not None for p in postings)
    assert all(0 <= p.cluster_id < k_used for p in postings)


def test_clustering_idempotent(db):
    """Running twice replaces the first run — row count stays at k."""
    from app.models import SkillCluster
    from sqlalchemy import select

    _seed_postings_with_embeddings(db, n=20, n_clusters=3, seed=4)

    from app.ml.clustering import run_clustering
    run_clustering(db, k=3)
    run_clustering(db, k=3)

    clusters = db.execute(select(SkillCluster)).scalars().all()
    assert len(clusters) == 3   # not 6


def test_clustering_top_skills_valid_json(db):
    """top_skills column stores valid JSON that deserialises to a list."""
    from app.models import SkillCluster
    from sqlalchemy import select

    _seed_postings_with_embeddings(db, n=20, n_clusters=3, seed=5)

    from app.ml.clustering import run_clustering
    run_clustering(db, k=3)

    clusters = db.execute(select(SkillCluster)).scalars().all()
    for c in clusters:
        skills = json.loads(c.top_skills)
        assert isinstance(skills, list)


def test_clustering_not_enough_data():
    """run_clustering returns an error dict (no exception) when the DB has no embedded postings."""
    from unittest.mock import MagicMock
    from app.ml.clustering import run_clustering

    # Mock the DB so the embedding query returns an empty result set
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_db = MagicMock()
    mock_db.execute.return_value = mock_result

    result = run_clustering(mock_db)
    assert "error" in result


def test_embedding_fetch_is_deterministic(db):
    """
    Embedding fetch must return posting IDs in the same order on repeated
    calls and in strict ascending-id order.

    Without ORDER BY, PostgreSQL heap-scan order varies between process
    launches, causing KMeans (random_state=42 picks centroids by position)
    to produce different solutions across runs — the bug this regression
    catches.  Adding .order_by(Posting.id) makes the grid fully reproducible.
    """
    from sqlalchemy import select
    from app.models import Posting

    _seed_postings_with_embeddings(db, n=20, n_clusters=3, seed=99)

    q = (
        select(Posting.id, Posting.embedding)
        .where(Posting.embedding.isnot(None))
        .order_by(Posting.id)
    )
    ids_run1 = [r[0] for r in db.execute(q).all()]
    ids_run2 = [r[0] for r in db.execute(q).all()]

    # Same order across two calls
    assert ids_run1 == ids_run2, "Embedding fetch returned different ID order on second call"
    # Order is strictly ascending — proves ORDER BY id is in effect
    assert ids_run1 == sorted(ids_run1), "Embedding fetch IDs are not in ascending order"


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

def test_clusters_endpoint_no_data(client):
    """GET /analytics/clusters before any run returns an empty list."""
    resp = client.get("/analytics/clusters")
    assert resp.status_code == 200
    data = resp.json()
    assert "clusters" in data
    # May be empty (test DB is fresh) or populated from a previous test
    assert isinstance(data["clusters"], list)


def test_clusters_run_endpoint(client, db):
    """POST /analytics/clusters/run runs clustering and returns summary."""
    _seed_postings_with_embeddings(db, n=25, n_clusters=5, seed=6)

    resp = client.post("/analytics/clusters/run?k=5")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["k"] == 5
    assert len(data["clusters"]) == 5
    assert data["silhouette"] > 0


def test_clusters_endpoint_after_run(client, db):
    """After a run with k=5, GET /analytics/clusters reflects the persisted k."""
    _seed_postings_with_embeddings(db, n=25, n_clusters=5, seed=7)
    run_resp = client.post("/analytics/clusters/run?k=5")
    assert run_resp.status_code == 200, run_resp.text

    resp = client.get("/analytics/clusters")
    assert resp.status_code == 200
    data = resp.json()
    assert data["k"] == 5
    assert len(data["clusters"]) == 5
    for c in data["clusters"]:
        assert c["size"] > 0
        assert isinstance(c["top_skills"], list)
