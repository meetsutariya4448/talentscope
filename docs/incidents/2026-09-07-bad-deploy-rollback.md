# Incident 2 — Bad deploy fails readiness, rolled back

**Date**: 2026-09-07, 01:33–01:37 UTC
**Type**: Deliberate recovery drill (induced, not a real outage)
**Environment**: `docker-compose.deploy.yml`, project `talentscope-deploy`
**Severity if real**: high — vector and hybrid search unavailable; FTS unaffected
**Detection**: immediate (deploy refused to complete) · **Recovery**: 35 s · **Data loss**: none

## Timeline

| Time (UTC) | Event |
|---|---|
| 01:33:20 | **Baseline**: healthy on `talentscope:b980722-dirty.f51dce48`, `/ready` all-green |
| 01:33:21 | `talentscope:broken` built — identical to the good image except `warm_model()` raises `RuntimeError("simulated bad deploy: incompatible model revision")` |
| 01:33:21 | **Bad deploy starts** |
| 01:35:54 | `scripts/deploy.sh` **exits 1** after 153 s: `/ready` never returned 200 |
| 01:35:54 | Observed: `/health` **200**, `/ready` **503**, vector search **503**, FTS search **200**, container marked `unhealthy` |
| 01:36:0x | `EmbeddingModelNotReady` and `ApiHighErrorRate` → pending |
| 01:36:31 | `scripts/rollback.sh` |
| 01:37:06 | **Recovered** on `f51dce48` — 35 s. `/ready` 200, vector 200, search returns 216 |

## The observation worth keeping

The failure mode was **partial, not total** — which is the realistic shape of a bad deploy
and precisely what the readiness work from `evals/coldstart.md` was built to handle:

```
/health                  -> 200   process alive, no restart loop
/ready                   -> 503   {"embedding_model": false}
/postings/?mode=vector   -> 503   refused, with Retry-After
/postings/?mode=fts      -> 200   still serving
```

Three things held simultaneously:

1. **Liveness stayed green, so nothing crash-looped.** Had `/health` checked the model,
   the orchestrator would have killed and restarted the container into the same failure
   indefinitely, and the logs would have been shredded across restarts.
2. **Readiness went red, so the broken instance advertised itself as unable to serve.**
   `deploy.sh` gates on exactly this and refused to mark the deploy successful.
3. **FTS kept working.** The failure was scoped to the paths that need the encoder, so
   the service degraded rather than disappeared.

## Root cause

Induced. The real signal was in the logs, with a full traceback:

```
2026-09-07 01:29:00,649 ERROR app.main Embedding model warmup failed; /ready will stay degraded
  raise RuntimeError("simulated bad deploy: incompatible model revision")
RuntimeError: simulated bad deploy: incompatible model revision
```

That line exists only because the warmup thread catches and logs rather than dying
silently, and because application loggers are configured at startup (`_configure_logging`
in `app/main.py`). Before that was added, `app.*` loggers had no handler and every one of
these messages was discarded — uvicorn configures only its own logger tree.

## Fix and recovery

```
bash scripts/rollback.sh
  running talentscope:broken, last verified-good is b980722-dirty.f51dce48
  (a failed deploy does not update .deploy-state) -> rolling back to b980722-dirty.f51dce48
  Rolled back to talentscope:b980722-dirty.f51dce48 in 35s
  {"status":"ok","checks":{"database":true,"redis":true,"embedding_model":true}}
```

Rollback recreates only `api`, `worker` and `beat` — Postgres and Redis are left running,
so a code-only fault does not become a data-layer outage as well.

## Two real defects the drill found

Both were found by running the drill, not by reading the scripts.

### 1. Rollback went one version too far

The first run rolled back to `485d00ba` when the last-known-good was `b07df57a`.

`deploy.sh` writes `.deploy-state` **only after** the deployment verifies healthy — which
is right. But it means a *failed* deploy leaves the state file describing the last good
version while a broken image is actually running. `rollback.sh` read `PREVIOUS_TAG` from
that file and skipped straight past the version it should have returned to.

Fixed: `rollback.sh` now reads the running image from the container and compares it
against `CURRENT_TAG`. If they differ, the recorded `CURRENT_TAG` *is* the last verified-
good version and becomes the target.

This is the kind of defect that only shows up when the rollback path is exercised after a
genuinely failed deploy, rather than tested by rolling back a successful one.

### 2. `set -u` broke the no-argument invocation

The fix above used `[ -n "$1" ]`, which under `set -u` is an unbound-variable error when
the script is called with no arguments — its most common form. The rollback exited 0
having done nothing, which is the worst possible failure for a recovery tool: it reports
success while leaving the outage in place. Fixed to `${1:-}`.

## What is explicitly not covered

**The database is not rolled back, and the script says so.** An Alembic migration that has
already run is not undone by pointing at an older image. Automatically running
`alembic downgrade` during an incident is how a bad deploy becomes data loss, so
`rollback.sh` prints the Alembic revision and states plainly that a migrating deploy needs
`scripts/restore_db.sh` instead.

The drill did not exercise that case — the broken image carried no migration.

## Follow-ups

- [ ] The bad version was caught by `deploy.sh`, not by an alert: `EmbeddingModelNotReady`
      has `for: 5m` (deliberately, so a normal ~2.5 s warmup never trips it) and the
      rollback happened first. Correct precedence, but it means the alert is a backstop
      for a deploy nobody is watching, not the primary signal.
- [ ] `ApiHighErrorRate` stayed firing for several minutes after recovery — expected, its
      `rate()` window is 5 m. Worth knowing before reading it during a real incident.
- [ ] No canary or staged rollout: the deploy replaces all replicas at once. `deploy.sh`
      refusing to record an unhealthy deployment limits the blast radius, but does not
      prevent it.
- [ ] `alembic current` reported `unknown` during rollback because the api container was
      unhealthy when queried. The revision should be captured *before* recreating.
