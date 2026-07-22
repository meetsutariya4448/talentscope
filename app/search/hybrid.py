"""
Hybrid search: FTS (GIN/tsvector) + dense vector (pgvector cosine), fused via
Reciprocal Rank Fusion.

Design notes
------------
* Both sub-queries pre-apply skill/location filters so fusion operates only on
  candidates that would survive the WHERE clauses anyway.
* RRF k=60 is the standard constant from Cormack et al. (2009); it smooths out
  position-1 dominance without requiring per-query tuning.
* Candidates per source (TOP_K=200) is intentionally larger than any single page
  so the fused ranking has enough material to paginate.  For a growing corpus,
  bump this rather than retuning k.
"""

from sqlalchemy.orm import Session
from sqlalchemy import text
import logging

logger = logging.getLogger(__name__)

TOP_K = 200   # candidates fetched from each source before fusion.
              # Empirically: at k=100 on a 1.5 k-post corpus, 4/5 test queries produced
              # different top-20 sets vs k=300 (2–3 entries changed). k=200 is the conservative
              # midpoint — revisit upward if corpus grows past ~20k postings.
RRF_K = 60    # smoothing constant — do not tune per-query


# ---------------------------------------------------------------------------
# Pure helpers — no DB, fully unit-testable
# ---------------------------------------------------------------------------

def reciprocal_rank_fusion(ranked_lists: list[list[int]], k: int = RRF_K) -> list[int]:
    """
    Merge ranked ID lists using Reciprocal Rank Fusion.

    score(d) = Σ  1 / (k + rank_i(d))   (rank is 1-indexed)

    Items appearing in multiple lists accumulate score; items in only one list
    still appear in the output so neither source is silenced.
    """
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda d: scores[d], reverse=True)


# ---------------------------------------------------------------------------
# Source-specific retrieval
# ---------------------------------------------------------------------------

def fts_search(
    db: Session,
    q: str,
    limit: int = TOP_K,
    skill: str = "",
    location: str = "",
) -> list[int]:
    """
    Return posting IDs ranked by ts_rank against the GIN search_vector index.
    Applies skill and location pre-filters so the candidate set matches the
    same constraints used in the final result page.
    """
    if not q:
        return []

    # Convert plainto_tsquery's AND semantics to OR so conversational queries
    # ("senior backend engineer with Kubernetes") don't require every term to appear.
    # plainto_tsquery handles stemming and stop-words; we just flip ' & ' → ' | '.
    # ts_rank still rewards more-term matches, so precision-oriented results rank higher.
    sql = """
        SELECT p.id
        FROM postings p
        WHERE p.search_vector @@ to_tsquery('english',
              replace(plainto_tsquery('english', :q)::text, ' & ', ' | '))
        {skill_join}
        {location_clause}
        ORDER BY ts_rank(p.search_vector,
                 to_tsquery('english',
                 replace(plainto_tsquery('english', :q)::text, ' & ', ' | '))) DESC
        LIMIT :limit
    """
    skill_join = ""
    location_clause = ""
    params: dict = {"q": q, "limit": limit}

    if skill:
        skill_join = (
            "AND EXISTS ("
            "  SELECT 1 FROM posting_skills ps"
            "  JOIN skills sk ON ps.skill_id = sk.id"
            "  WHERE ps.posting_id = p.id AND lower(sk.name) = :skill"
            ")"
        )
        params["skill"] = skill.lower()

    if location:
        location_clause = "AND p.location ILIKE :location"
        params["location"] = f"%{location}%"

    rows = db.execute(
        text(sql.format(skill_join=skill_join, location_clause=location_clause)),
        params,
    ).fetchall()
    return [r[0] for r in rows]


def vector_search(
    db: Session,
    query_text: str,
    limit: int = TOP_K,
    skill: str = "",
    location: str = "",
) -> list[int]:
    """
    Return posting IDs ranked by cosine distance (ascending) using pgvector.
    Only considers postings that already have an embedding.
    """
    from app.search.encoder import get_model
    vec = get_model().encode(query_text, normalize_embeddings=True).tolist()
    vec_str = "[" + ",".join(str(v) for v in vec) + "]"

    sql = """
        SELECT p.id
        FROM postings p
        WHERE p.embedding IS NOT NULL
        {skill_join}
        {location_clause}
        ORDER BY p.embedding <=> CAST(:vec AS vector)
        LIMIT :limit
    """
    skill_join = ""
    location_clause = ""
    params: dict = {"vec": vec_str, "limit": limit}

    if skill:
        skill_join = (
            "AND EXISTS ("
            "  SELECT 1 FROM posting_skills ps"
            "  JOIN skills sk ON ps.skill_id = sk.id"
            "  WHERE ps.posting_id = p.id AND lower(sk.name) = :skill"
            ")"
        )
        params["skill"] = skill.lower()

    if location:
        location_clause = "AND p.location ILIKE :location"
        params["location"] = f"%{location}%"

    rows = db.execute(
        text(sql.format(skill_join=skill_join, location_clause=location_clause)),
        params,
    ).fetchall()
    return [r[0] for r in rows]


def fetch_postings_by_ids(db: Session, ids: list[int]) -> list[tuple]:
    """
    Fetch full posting + company_name rows for a list of IDs, preserving the
    caller's ordering (used after RRF to maintain fused rank).
    """
    if not ids:
        return []

    # array_position preserves the RRF order without a second sort
    rows = db.execute(
        text("""
            SELECT p.id, p.title, p.location, p.salary_min, p.salary_max,
                   p.currency, p.source, p.url, p.posted_at, c.name AS company_name
            FROM postings p
            LEFT JOIN companies c ON p.company_id = c.id
            WHERE p.id = ANY(:ids)
            ORDER BY array_position(:ids, p.id)
        """),
        {"ids": ids},
    ).fetchall()
    return rows
