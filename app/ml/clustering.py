"""
KMeans role clustering on pgvector embeddings.

Pipeline
--------
1. Pull all postings that have an embedding.
2. Normalize vectors to the unit sphere (they were encoded with
   normalize_embeddings=True but we renormalize defensively).
3. Select k by silhouette score over k = 5..15 using a random
   subsample of min(N, 500) for speed.
4. Fit KMeans (n_init=10, random_state=42 for reproducibility).
5. For each cluster, count skill frequency across its postings and
   build a short human-readable label from the top-2 skills.
6. Persist results: clear the old skill_clusters rows, insert new
   rows, update postings.cluster_id in bulk.

Interview notes
---------------
* KMeans over HDBSCAN: corpus is ~1.5k dense embeddings — KMeans is
  faster and more explainable at this scale.
* k via silhouette: grid over 5..15, pick argmax.  Silhouette ranges
  –1..1; values ≥ 0.15 on high-dimensional embeddings are considered
  good (Rousseeuw 1987).
* Near-identical postings (same role, different office location) land in
  the same cluster — correct semantically; cluster size ≠ distinct
  headcount if such duplicates exist.
"""

import json
import logging
from collections import Counter
from datetime import datetime

import numpy as np

logger = logging.getLogger(__name__)

_K_MIN = 5
_K_MAX = 15
_SEED  = 42
_SIL_SAMPLE = 500   # cap for silhouette subsample (speed)


# ---------------------------------------------------------------------------
# k selection
# ---------------------------------------------------------------------------

def _best_k(embeddings: np.ndarray) -> tuple[int, dict[int, float]]:
    """Return (best_k, {k: silhouette_score}) over _K_MIN.._K_MAX."""
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score

    n = len(embeddings)
    k_max = min(_K_MAX, n // 2)   # can't have more clusters than n/2
    k_min = min(_K_MIN, k_max - 1)

    scores: dict[int, float] = {}
    sample = min(n, _SIL_SAMPLE)

    for k in range(k_min, k_max + 1):
        km = KMeans(n_clusters=k, random_state=_SEED, n_init=10)
        labels = km.fit_predict(embeddings)
        sil = silhouette_score(embeddings, labels, sample_size=sample, random_state=_SEED)
        scores[k] = float(sil)
        logger.debug("k=%d  silhouette=%.4f", k, sil)

    best = max(scores, key=lambda k: scores[k])
    return best, scores


# ---------------------------------------------------------------------------
# Label generation
# ---------------------------------------------------------------------------

def _cluster_top_skills(
    cluster_member_ids: dict[int, list[int]],
    db,
    n: int = 5,
) -> dict[int, list[str]]:
    """
    For each cluster_id → list[posting_id], return the top-n skill names
    by frequency within that cluster.
    """
    from sqlalchemy import text

    all_ids = [pid for ids in cluster_member_ids.values() for pid in ids]
    if not all_ids:
        return {k: [] for k in cluster_member_ids}

    rows = db.execute(
        text("""
            SELECT ps.posting_id, s.name
            FROM posting_skills ps
            JOIN skills s ON ps.skill_id = s.id
            WHERE ps.posting_id = ANY(:ids)
        """),
        {"ids": all_ids},
    ).fetchall()

    # posting_id → cluster_id reverse map
    pid_to_cluster: dict[int, int] = {}
    for cid, pids in cluster_member_ids.items():
        for pid in pids:
            pid_to_cluster[pid] = cid

    cluster_skill_counts: dict[int, Counter] = {cid: Counter() for cid in cluster_member_ids}
    for posting_id, skill_name in rows:
        cid = pid_to_cluster.get(posting_id)
        if cid is not None:
            cluster_skill_counts[cid][skill_name] += 1

    return {
        cid: [name for name, _ in counter.most_common(n)]
        for cid, counter in cluster_skill_counts.items()
    }


def _make_label(top_skills: list[str]) -> str:
    if not top_skills:
        return "Uncategorized"
    return " · ".join(top_skills[:2])


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def run_clustering(db, k: int | None = None) -> dict:
    """
    Cluster all embedded postings and persist results.

    Returns a summary dict with k, silhouette, per-cluster info, and counts.
    Idempotent: each call replaces the previous clustering run.
    """
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score
    from sklearn.preprocessing import normalize
    from sqlalchemy import select, text
    from app.models import Posting, SkillCluster

    # --- 1. Pull embeddings ---
    rows = db.execute(
        select(Posting.id, Posting.embedding).where(Posting.embedding.isnot(None))
    ).all()

    n = len(rows)
    if n < max(10, _K_MIN * 2):
        return {"error": f"Not enough embedded postings to cluster (have {n}, need ≥ {max(10, _K_MIN * 2)})"}

    posting_ids = [r[0] for r in rows]
    raw_vecs    = np.array([list(r[1]) for r in rows], dtype=np.float32)
    embeddings  = normalize(raw_vecs)   # unit sphere

    # --- 2. Choose k ---
    sil_scores: dict[int, float] = {}
    if k is None:
        k, sil_scores = _best_k(embeddings)
        logger.info("Selected k=%d (silhouette=%.4f)", k, sil_scores[k])

    # --- 3. Fit KMeans ---
    km     = KMeans(n_clusters=k, random_state=_SEED, n_init=10)
    labels = km.fit_predict(embeddings)
    final_sil = float(silhouette_score(
        embeddings, labels,
        sample_size=min(n, _SIL_SAMPLE),
        random_state=_SEED,
    ))
    logger.info("KMeans k=%d  silhouette=%.4f  n=%d", k, final_sil, n)

    # --- 4. Group postings by cluster ---
    cluster_members: dict[int, list[int]] = {i: [] for i in range(k)}
    for pid, label in zip(posting_ids, labels):
        cluster_members[int(label)].append(pid)

    # --- 5. Compute top skills per cluster ---
    cluster_top = _cluster_top_skills(cluster_members, db)

    # --- 6. Persist ---
    run_at = datetime.utcnow()

    db.execute(text("DELETE FROM skill_clusters"))

    cluster_rows = []
    for cid in range(k):
        top = cluster_top.get(cid, [])
        cluster_rows.append(SkillCluster(
            cluster_id = cid,
            label      = _make_label(top),
            size       = len(cluster_members[cid]),
            top_skills = json.dumps(top),
            silhouette = round(final_sil, 6),
            run_at     = run_at,
        ))
    db.add_all(cluster_rows)

    # Bulk-update posting.cluster_id
    for label_int, pid in zip(labels.tolist(), posting_ids):
        db.execute(
            text("UPDATE postings SET cluster_id = :cid WHERE id = :pid"),
            {"cid": int(label_int), "pid": pid},
        )

    db.commit()

    summary = {
        "k":          k,
        "n":          n,
        "silhouette": round(final_sil, 4),
        "sil_by_k":   {str(ki): round(v, 4) for ki, v in sil_scores.items()},
        "run_at":     run_at.isoformat(),
        "clusters": sorted(
            [
                {
                    "cluster_id": cid,
                    "label":      _make_label(cluster_top.get(cid, [])),
                    "size":       len(cluster_members[cid]),
                    "top_skills": cluster_top.get(cid, []),
                }
                for cid in range(k)
            ],
            key=lambda c: c["size"],
            reverse=True,
        ),
    }
    return summary
