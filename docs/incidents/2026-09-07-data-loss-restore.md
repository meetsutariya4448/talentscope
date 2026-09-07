# Incident 1 — Postings corpus lost, restored from backup

**Date**: 2026-09-07, 01:22–01:26 UTC
**Type**: Deliberate recovery drill (induced, not a real outage)
**Environment**: `docker-compose.deploy.yml`, project `talentscope-deploy`,
image `talentscope:b980722-dirty.b07df57a`
**Severity if real**: critical — total loss of searchable data
**Detection**: 2 min 21 s · **Recovery**: 2 s restore, ~17 s to green · **Data loss**: none

## Timeline

| Time (UTC) | Event |
|---|---|
| 01:18:54 | Backup taken — `postings=232 companies=10 skills=116 embedded=232`, 529,465 bytes, verified in `s3://talentscope-dev-backups/` |
| 01:22:11 | **Baseline**: `/ready` 200 all-green, hybrid search returns 216 results, 232 rows |
| 01:22:12 | **Failure induced**: `TRUNCATE postings CASCADE` (cascaded to `applications`) |
| 01:22:12 | `/ready` still **200, all three checks green**. Search returns **0**. |
| 01:22:32 | `PostingsCorpusEmpty` → pending |
| 01:24:27 | `PostingsCorpusEmpty` → **firing**, delivered to Alertmanager |
| 01:26:04 | `scripts/restore_db.sh --latest --yes` |
| 01:26:06 | Restore complete — row counts match manifest exactly |
| 01:26:21 | api restarted, `/ready` 200, search returns 216 |
| 01:26:29 | Alert resolved |

## The observation worth keeping

**`/ready` returned 200 with every check green while the database was empty.**

```
01:22:12  /ready -> 200 {"status":"ok","checks":{"database":true,"redis":true,"embedding_model":true}}
01:22:12  search -> 200 {"total": 0}
```

This is correct behaviour, not a bug in the probe. `/ready` answers "can this instance
serve traffic" — the connection was fine, Redis was fine, the model was warm. It has no
opinion about whether the data is *right*. Health checks cannot detect data loss, and an
instance that is perfectly healthy and serving zero results will never be caught by a
liveness or readiness probe.

That gap is why `PostingsCorpusEmpty` exists, and it is a metric-based alert
(`postings_total{job="talentscope-worker"} == 0`) rather than a probe.

## Root cause

Induced: `TRUNCATE postings CASCADE`. The realistic equivalents are a mis-scoped
migration, a `DELETE` without a `WHERE`, or a restore into the wrong database.

Worth noting: the truncation **cascaded to `applications`** — the user's own job-application
log, which no one would think of as part of "the postings table". A destructive statement
aimed at one table took a second one with it.

## Fix and recovery

```
bash scripts/restore_db.sh --latest --yes
  downloading s3://talentscope-dev-backups/talentscope-talentscope-20260907T011854Z-b980722.dump.gz
  manifest says: postings=232 companies=10 skills=116 embedded=232
  terminating existing connections
  dropping and recreating talentscope
  restoring
  restored: postings=232 companies=10 skills=116 embedded=232
  expected: postings=232 companies=10 skills=116 embedded=232
  row counts match the manifest.
```

Restore itself took **2 seconds** for a 529 KB compressed dump of 232 postings. That
number does not extrapolate — it is a small corpus.

Two details that mattered:

1. **Connections had to be terminated first.** `DROP DATABASE` fails while the api's
   SQLAlchemy pool holds connections, and the pool reconnects immediately, so stopping
   traffic is not sufficient. `restore_db.sh` runs `pg_terminate_backend` first.
2. **The api had to be restarted afterwards.** Its pool still referenced the dropped
   database. Between restore and restart, `/ready` reported 200 — a *second* instance of
   the same lesson, from the other direction: the pool handed out connections that
   answered `SELECT 1` against a database that had just been replaced underneath it.

## What the drill changed

- **Verification is not optional.** `restore_db.sh` compares restored row counts against
  a manifest stored beside the dump. "pg_restore exited 0" is not evidence that the right
  data came back; the manifest comparison is.
- **`PostingsCorpusEmpty` had to be scoped by job.** Written unqualified, it matched a
  phantom `postings_total{job="talentscope-api"} = 0` series — the api registers the gauge
  but deliberately never refreshes worker-owned pull metrics — and fired continuously
  against a healthy database. An alert that is always firing is an alert nobody reads.
  Fixed to `postings_total{job="talentscope-worker"}` before the drill.

## Follow-ups

- [ ] Backups are manual. A scheduled backup (Celery beat entry or a sidecar) is the
      obvious next step; the retention side already exists as a lifecycle rule on the
      bucket (`terraform/storage.tf`).
- [ ] Restore was exercised against a 232-row corpus. The timing says nothing about a
      corpus two orders of magnitude larger.
- [ ] No point-in-time recovery. The RDS path in `terraform/database.tf` has
      `backup_retention_period = 7`, but this drill used `pg_dump` snapshots, so the
      recovery point is the last dump, not the last transaction.
