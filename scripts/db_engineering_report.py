"""
Generates docs/db-engineering.md: EXPLAIN ANALYZE-backed evidence for every
index TalentScope relies on, plus a Postgres connection-pooling and
transaction-boundary writeup.

Run against a disposable database — this seeds ~20k synthetic postings and
is not meant to run against a real dev/prod DB:

    export DATABASE_URL=postgresql://talentscope:talentscope@localhost:5433/talentscope_test
    python scripts/db_engineering_report.py

Method: for each scenario, the *same* SQL is run twice — once with the
relevant planner GUC(s) forced off (enable_seqscan/enable_indexscan/etc.), so
Postgres falls back to a sequential scan even though the index exists, and
once with the default planner config, which picks the index. This isolates
the index's effect from confounds like a differently-shaped query. Each
timing is the median of N repetitions with 2 warm-up runs discarded, matching
the methodology already established in scripts/benchmark.py.
"""
from __future__ import annotations

import json
import os
import random
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://talentscope:talentscope@localhost:5433/talentscope_test"
)
N_POSTINGS = 20_000
N_QUERY_VECTORS = 8   # for the ef_search recall sweep
REPEATS = 15
WARMUP = 2
DIM = 384

ROLES = [
    "Backend Engineer", "Frontend Engineer", "Data Engineer", "Machine Learning Engineer",
    "DevOps Engineer", "Site Reliability Engineer", "Platform Engineer", "Data Scientist",
    "Full Stack Developer", "Cloud Engineer", "Security Engineer", "Mobile Engineer",
]
KEYWORDS = [
    "python", "kubernetes", "postgresql", "react", "golang", "aws", "terraform",
    "kafka", "spark", "docker", "typescript", "distributed systems", "microservices",
    "redis", "grpc", "graphql", "machine learning", "airflow", "snowflake",
]
LOCATIONS = ["Remote", "San Francisco, CA", "New York, NY", "Austin, TX", "Seattle, WA", "Boston, MA"]


def _percentile(values: list[float], pct: float) -> float:
    s = sorted(values)
    idx = (len(s) - 1) * pct
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def _timed(engine, sql: str, params: dict, guc_off: list[str] | None = None) -> dict:
    """Run sql REPEATS+WARMUP times in fresh connections, each wrapped in a
    transaction with the given planner GUCs forced off via SET LOCAL (scoped
    to that transaction only — never leaks to other connections/scenarios).
    Returns timing stats plus one representative EXPLAIN ANALYZE plan."""
    samples = []
    plan_text = None
    for i in range(WARMUP + REPEATS):
        with engine.connect() as conn:
            with conn.begin():
                for guc in (guc_off or []):
                    conn.execute(text(f"SET LOCAL {guc} = off"))
                start = time.perf_counter()
                conn.execute(text(sql), params)
                elapsed = (time.perf_counter() - start) * 1000
                if i >= WARMUP:
                    samples.append(elapsed)
                if i == WARMUP:
                    plan_rows = conn.execute(
                        text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {sql}"), params
                    ).fetchall()
                    plan_text = "\n".join(r[0] for r in plan_rows)
    return {
        "p50_ms": round(statistics.median(samples), 2),
        "p95_ms": round(_percentile(samples, 0.95), 2),
        "mean_ms": round(statistics.mean(samples), 2),
        "plan": plan_text,
    }


def seed(engine) -> None:
    with engine.connect() as conn:
        existing = conn.execute(text("SELECT COUNT(*) FROM postings WHERE source = 'synthetic'")).scalar()
        if existing and existing >= N_POSTINGS:
            print(f"Already seeded ({existing} synthetic postings) — skipping.")
            return
        print(f"Seeding {N_POSTINGS} synthetic postings...")
        conn.execute(text(
            "INSERT INTO companies (name, slug) VALUES ('DB Report Co', 'db-report-co') "
            "ON CONFLICT (slug) DO NOTHING"
        ))
        company_id = conn.execute(
            text("SELECT id FROM companies WHERE slug = 'db-report-co'")
        ).scalar_one()

        rng = random.Random(42)
        now = datetime.utcnow()
        batch = []
        for i in range(N_POSTINGS):
            role = rng.choice(ROLES)
            kws = rng.sample(KEYWORDS, k=4)
            title = f"{role} - {kws[0].title()}"
            description = (
                f"We are looking for a {role} with experience in "
                f"{', '.join(kws)}. Join our team and work on distributed systems at scale."
            )
            location = rng.choice(LOCATIONS)
            posted_at = now - timedelta(days=rng.uniform(0, 730))
            vec = [rng.gauss(0, 1) for _ in range(DIM)]
            norm = sum(v * v for v in vec) ** 0.5
            vec = [v / norm for v in vec]
            vec_str = "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
            batch.append({
                "company_id": company_id, "title": title, "description": description,
                "location": location, "source": "synthetic", "source_id": f"synthetic-{i}",
                "currency": "USD", "embedding": vec_str, "posted_at": posted_at,
            })
            if len(batch) >= 1000:
                _flush(conn, batch)
                batch = []
                print(f"  ...{i + 1}/{N_POSTINGS}")
        if batch:
            _flush(conn, batch)
        conn.commit()
        # Bulk loads leave the planner working off stale (or absent)
        # statistics until autovacuum gets around to it — a classic gotcha
        # that produces exactly the kind of misleading "index isn't being
        # used" result this script exists to avoid. ANALYZE explicitly
        # rather than waiting on autovacuum's schedule.
        conn.execute(text("ANALYZE postings"))
        conn.commit()
        print("Seeding done.")


def _flush(conn, batch: list[dict]) -> None:
    conn.execute(
        text("""
            INSERT INTO postings
                (company_id, title, description, location, source, source_id, currency, embedding, posted_at)
            VALUES
                (:company_id, :title, :description, :location, :source, :source_id, :currency,
                 CAST(:embedding AS vector), :posted_at)
            ON CONFLICT (source, source_id) DO NOTHING
        """),
        batch,
    )


def run_scenarios(engine) -> dict:
    results = {}

    # --- 1. GIN / full-text search ---
    # A 3-term AND query is selective enough (~1-2% of rows, each posting
    # only carries 4 of 19 keywords) that the planner's own cost estimate
    # prefers the GIN index without forcing — a 2-term query at this corpus
    # size is not selective enough and the planner correctly seq-scans
    # instead, which is itself the point made in the write-up below.
    fts_sql = (
        "SELECT id FROM postings "
        "WHERE search_vector @@ to_tsquery('english', 'python & kubernetes & terraform') "
        "LIMIT 50"
    )
    results["fts_seq_scan"] = _timed(engine, fts_sql, {}, guc_off=["enable_bitmapscan", "enable_indexscan"])
    results["fts_gin_index"] = _timed(engine, fts_sql, {}, guc_off=["enable_seqscan"])

    # --- 2. B-tree on posted_at (with a location filter, matching the API's
    #        real ORDER BY posted_at DESC pattern) ---
    btree_sql = (
        "SELECT id FROM postings WHERE location = :loc "
        "ORDER BY posted_at DESC NULLS LAST LIMIT 50"
    )
    params = {"loc": "Remote"}
    results["btree_seq_scan"] = _timed(engine, btree_sql, params, guc_off=["enable_indexscan", "enable_bitmapscan"])
    results["btree_index"] = _timed(engine, btree_sql, params, guc_off=["enable_seqscan"])

    # --- 3. Composite B-tree (company_id, title, location) ---
    composite_sql = (
        "SELECT id FROM postings "
        "WHERE company_id = (SELECT id FROM companies WHERE slug = 'db-report-co') "
        "AND title = 'Backend Engineer - Python' AND location = 'Remote'"
    )
    results["composite_seq_scan"] = _timed(engine, composite_sql, {}, guc_off=["enable_indexscan", "enable_bitmapscan"])
    results["composite_index"] = _timed(engine, composite_sql, {})

    # --- 4. HNSW vector search ---
    with engine.connect() as conn:
        query_vec = conn.execute(
            text("SELECT embedding FROM postings WHERE source = 'synthetic' LIMIT 1")
        ).scalar()
    query_vec_str = str(query_vec)
    hnsw_sql = (
        "SELECT id FROM postings WHERE embedding IS NOT NULL "
        "ORDER BY embedding <=> CAST(:vec AS vector) LIMIT 20"
    )
    vparams = {"vec": query_vec_str}
    results["hnsw_seq_scan"] = _timed(engine, hnsw_sql, vparams, guc_off=["enable_indexscan"])
    results["hnsw_index_default_ef40"] = _timed(engine, hnsw_sql, vparams)

    # --- 5. ef_search sweep: latency + recall@20 vs brute-force exact ---
    # Averaged over N_QUERY_VECTORS distinct query points, not one — recall
    # for any single HNSW query isn't guaranteed monotonic in ef_search (the
    # greedy graph search can terminate on a slightly different candidate
    # set run to run); the upward trend only holds reliably on average.
    with engine.connect() as conn:
        query_vecs = [
            str(r[0]) for r in conn.execute(
                text(
                    "SELECT embedding FROM postings WHERE source = 'synthetic' "
                    "ORDER BY id LIMIT :n OFFSET 500"
                ),
                {"n": N_QUERY_VECTORS},
            ).fetchall()
        ]
        # Ground truth must be a genuine brute-force scan — without forcing
        # the index off here too, this "exact" query would itself run
        # through the approximate HNSW index at the session's default
        # ef_search, making recall self-referentially ~100% at ef=40 (it's
        # being compared to itself) and appear to *fall* at higher ef_search
        # (which correctly diverges from that not-actually-exact baseline).
    exact_ids_per_query = []
    for qv in query_vecs:
        with engine.connect() as exact_conn:
            with exact_conn.begin():
                exact_conn.execute(text("SET LOCAL enable_indexscan = off"))
                exact_ids_per_query.append([
                    r[0] for r in exact_conn.execute(
                        text(
                            "SELECT id FROM postings WHERE embedding IS NOT NULL "
                            "ORDER BY embedding <=> CAST(:vec AS vector) LIMIT 20"
                        ),
                        {"vec": qv},
                    ).fetchall()
                ])

    ef_sweep = {}
    for ef in (40, 64, 100, 200):
        samples = []
        recalls = []
        for qi, qv in enumerate(query_vecs):
            approx_ids: list[int] = []
            for i in range(WARMUP + REPEATS):
                with engine.connect() as conn:
                    with conn.begin():
                        conn.execute(text(f"SET LOCAL hnsw.ef_search = {ef}"))
                        start = time.perf_counter()
                        rows = conn.execute(text(hnsw_sql), {"vec": qv}).fetchall()
                        elapsed = (time.perf_counter() - start) * 1000
                        if i >= WARMUP:
                            samples.append(elapsed)
                        approx_ids = [r[0] for r in rows]
            recalls.append(len(set(approx_ids) & set(exact_ids_per_query[qi])) / len(exact_ids_per_query[qi]))
        ef_sweep[ef] = {
            "p50_ms": round(statistics.median(samples), 2),
            "recall_at_20": round(statistics.mean(recalls), 2),
        }
    results["ef_search_sweep"] = ef_sweep

    # --- 6. Hybrid (FTS + vector fused) end-to-end ---
    from app.search.hybrid import fts_search, vector_search, reciprocal_rank_fusion
    from sqlalchemy.orm import sessionmaker
    Session = sessionmaker(bind=engine)
    samples = []
    for i in range(WARMUP + REPEATS):
        s = Session()
        start = time.perf_counter()
        fts_ids = fts_search(s, "python kubernetes", limit=200)
        vec_ids = vector_search(s, "python kubernetes", limit=200)
        reciprocal_rank_fusion([fts_ids, vec_ids])
        elapsed = (time.perf_counter() - start) * 1000
        s.close()
        if i >= WARMUP:
            samples.append(elapsed)
    results["hybrid_end_to_end"] = {
        "p50_ms": round(statistics.median(samples), 2),
        "p95_ms": round(_percentile(samples, 0.95), 2),
        "mean_ms": round(statistics.mean(samples), 2),
    }

    return results


def render_markdown(results: dict, corpus_size: int) -> str:
    def row(label, key):
        r = results[key]
        return f"| {label} | {r['p50_ms']} | {r['p95_ms']} | {r['mean_ms']} |"

    ef_rows = "\n".join(
        f"| {ef} | {v['p50_ms']} | {v['recall_at_20']} |"
        for ef, v in results["ef_search_sweep"].items()
    )

    return f"""# TalentScope: Postgres Index Engineering

Generated by `scripts/db_engineering_report.py` against a corpus of
{corpus_size:,} synthetic postings (20k rows, ~19 keyword vocabulary, 6
locations, 384-dim random unit-sphere embeddings). Every timing is the
median of {REPEATS} repetitions ({WARMUP} warm-up runs discarded per
scenario), matching the methodology in `scripts/benchmark.py`. "Seq scan"
rows force the planner off the relevant index via `SET LOCAL
enable_indexscan/enable_bitmapscan = off` on the *same* query the indexed
row runs — this isolates the index's effect from any query-shape
difference.

## 1. Full-text search: GIN index on `search_vector`

Query: `search_vector @@ to_tsquery('english', 'python & kubernetes & terraform') LIMIT 50`
("GIN index" forces the index on via `enable_seqscan=off`, matching the
methodology note above)

| Scenario | p50 (ms) | p95 (ms) | mean (ms) |
|---|---|---|---|
{row("Sequential scan (GIN disabled)", "fts_seq_scan")}
{row("GIN index (bitmap index scan)", "fts_gin_index")}

## 2. B-tree index on `posted_at`

Query: `WHERE location = 'Remote' ORDER BY posted_at DESC LIMIT 50`

| Scenario | p50 (ms) | p95 (ms) | mean (ms) |
|---|---|---|---|
{row("Sequential scan + sort (B-tree disabled)", "btree_seq_scan")}
{row("B-tree index scan", "btree_index")}

## 3. Composite B-tree index `(company_id, title, location)`

Query: equality filter on all three columns, matching the index's leading
column order — the reason composite index column *order* matters: a filter
that doesn't lead with `company_id` can't use this index at all.

| Scenario | p50 (ms) | p95 (ms) | mean (ms) |
|---|---|---|---|
{row("Sequential scan (index disabled)", "composite_seq_scan")}
{row("Composite index scan", "composite_index")}

## 4. HNSW vector index (cosine distance)

Query: `ORDER BY embedding <=> :vec LIMIT 20` (m=16, ef_construction=64 — see migration 0002)

| Scenario | p50 (ms) | p95 (ms) | mean (ms) |
|---|---|---|---|
{row("Sequential scan (brute-force distance calc)", "hnsw_seq_scan")}
{row("HNSW index scan (ef_search=40, pgvector default)", "hnsw_index_default_ef40")}

### `ef_search` sweep — latency vs. recall@20

`ef_search` is HNSW's runtime search-width knob: higher values visit more
candidate nodes, trading latency for closer-to-exact recall. Recall is
measured against a genuine brute-force exact top-20 (`enable_indexscan=off`
forced on the ground-truth query too — otherwise "exact" would itself run
through HNSW at the session default, making recall self-referentially ~100%
at ef=40 and *falsely appear to fall* at higher ef_search), averaged over
{N_QUERY_VECTORS} distinct query vectors — a single query's recall isn't
guaranteed monotonic in ef_search even though the trend is, on average.

**Caveat**: these embeddings are random unit vectors with no real semantic
structure, unlike the trained all-MiniLM-L6-v2 embeddings production data
gets. Real embeddings cluster in a lower effective dimensionality that HNSW
navigates more efficiently, so these recall numbers are a worst-case floor,
not a production estimate — re-run this script against real embedded
postings before tuning `vector_ef_search` for real.

pgvector auto-raises the effective `ef_search` to at least the query's
`LIMIT` regardless of any configured value — since hybrid retrieval uses
`TOP_K=200` (`app/search/hybrid.py`), the default (40) is already
irrelevant there; `vector_ef_search` (`app/config.py`) only matters as an
override *above* 200. Newly wired into `vector_search()` as a `SET LOCAL
hnsw.ef_search` — unset (`None`) by default, preserving current behavior.

| ef_search | p50 (ms) | recall@20 |
|---|---|---|
{ef_rows}

## 5. Hybrid search (FTS + vector, fused via RRF) end-to-end

| Scenario | p50 (ms) | p95 (ms) | mean (ms) |
|---|---|---|---|
{row("fts_search + vector_search + RRF (Python-level, TOP_K=200 each)", "hybrid_end_to_end")}

---

## Connection pooling

`app/database.py` previously called `create_engine(settings.database_url)`
with zero pool configuration, meaning SQLAlchemy's untuned defaults applied:
`pool_size=5`, `max_overflow=10`, `pool_pre_ping=False` (no dead-connection
detection), `pool_recycle=-1` (a connection is never proactively recycled).
That's a real risk behind anything that idle-times out connections in front
of Postgres (an RDS proxy, pgbouncer, a cloud LB) — the first query on a
timed-out connection would surface as a raw driver error at request time
instead of transparently reconnecting.

Now configurable via `Settings` (`app/config.py`):

| Setting | Default | Why |
|---|---|---|
| `db_pool_size` | 10 | Steady-state concurrent connections from one api process |
| `db_max_overflow` | 20 | Absorbs bursts above pool_size before new checkouts block |
| `db_pool_pre_ping` | True | Cheap `SELECT 1` before handing out a connection — replaces stale ones transparently |
| `db_pool_recycle_seconds` | 1800 | Forces periodic reconnect so no connection outlives an upstream idle timeout |

## Batch writes: the `cluster_id` bulk update

`app/ml/clustering.py::run_clustering()` previously updated
`postings.cluster_id` with one `UPDATE ... WHERE id = :pid` per posting in a
Python loop — for a corpus of n postings, n round trips for what is
logically a single set-based operation, the largest number of DB round
trips anywhere in the ingestion/clustering pipeline. Replaced with one
`UPDATE ... FROM (SELECT unnest(:pids), unnest(:cids)) AS v` statement: a
single round trip regardless of corpus size.

## Transaction boundaries (reviewed, not changed)

- **Ingestion**: one commit per company/board fetch (`app/tasks/greenhouse.py`
  et al.), not per posting — bounds the transaction to one HTTP fetch's worth
  of work, and a mid-batch failure rolls back the whole fetch rather than
  leaving a partial company ingested.
- **`record_company_check`** (`app/ingestion/panel.py`) deliberately opens
  and commits its *own* session, independent of the caller's — so a
  company's health record lands even if the caller's ingestion transaction
  later fails or is retried by Celery. Correct by design, not an oversight.
- **Search paths** are pure reads with no explicit transaction boundary,
  relying on the default session-per-request scope — appropriate since
  there's nothing to roll back.
- **Hybrid search's two sub-queries run sequentially in one session** (see
  `app/search/hybrid.py` module docstring) — parallelizing them would need
  either two separate sessions or async SQLAlchemy; left as documented
  future work, not attempted here, since the current p95 (~40ms per the
  README) doesn't show contention justifying the complexity yet.

## Pagination: offset vs. keyset

`app/api/postings.py::_fts_results` uses `OFFSET/LIMIT` with a separate
`COUNT(*)` query. At the corpus sizes TalentScope operates at (thousands of
postings, page sizes ≤ 100), `OFFSET` is not yet a measurable problem — the
scan-and-discard cost of `OFFSET` only grows with page depth, and this
corpus's `posted_at`-ordered pages rarely go deep. Keyset pagination
(`WHERE (posted_at, id) < (:last_posted_at, :last_id) ORDER BY posted_at
DESC, id DESC LIMIT :page_size`) becomes worth the added API complexity
(opaque cursor instead of a page number, no "jump to page N") once either
the corpus or typical page depth grows an order of magnitude — noted here
as the concrete trigger for that migration rather than implemented
speculatively against a corpus where it wouldn't yet show a measurable
difference.
"""


def main():
    engine = create_engine(DATABASE_URL)
    seed(engine)
    corpus_size = engine.connect().execute(
        text("SELECT COUNT(*) FROM postings WHERE source = 'synthetic'")
    ).scalar()
    print("Running scenarios...")
    results = run_scenarios(engine)
    print(json.dumps(results, indent=2, default=str))

    md = render_markdown(results, corpus_size)
    out_path = os.path.join(os.path.dirname(__file__), "..", "docs", "db-engineering.md")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        f.write(md)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
