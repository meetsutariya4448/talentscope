# Cost of this exercise

**Date**: 2026-09-07

## Actual cloud spend: $0.00

**No AWS account was used.** Every AWS resource in `terraform/` was applied against
LocalStack Community running in a local container (`localstack/localstack:3.0`), and the
whole application ran on local Docker. Nothing was billed, and there is no invoice to
reconcile against.

That is a deliberate scope decision, not an oversight, and it bounds what this exercise
proves. The deployment mechanics — tagged immutable images, secrets read from Secrets
Manager, least-privilege IAM, backup to S3 with verified restore, rollback, teardown —
were genuinely exercised. What was **not** exercised is anything that only a real account
can show: actual instance behaviour under a real hypervisor, real network latency and
egress, real IAM enforcement (LocalStack does not enforce policies), RDS, autoscaling, and
a real bill.

## Measured local consumption

The real cost of this exercise was local machine resources and time, which is what can be
measured honestly here.

| | |
|---|---|
| Wall clock, end to end | ~4 hours (setup, benchmarks, deploy, two drills) |
| Docker VM allocation | 10 CPUs, 8.32 GB RAM |
| Application image | **1.87 GB** per tag (was 1.94 GB before this work; see below) |
| Image tags retained | 5 (4 deploy tags + `talentscope:broken`) |
| Docker images total | 7.38 GB (1.63 GB reclaimable) |
| Build cache | 5.35 GB (3.44 GB reclaimable) |
| Named volumes | 297.5 MB across 16 volumes |
| Postgres data volume | 223.7 MB |
| Database backup | 529 KB compressed (232 postings, `pg_dump -Fc \| gzip -9`) |
| Peak worker RSS | 380–420 MB per prefork child |
| Peak API RSS | ~460 MB |

### Image size

The image is **1.87 GB**, slightly *smaller* than the 1.94 GB recorded in `README.md`,
despite now embedding the ~90 MB sentence-transformers model. Two changes paid for it:

- `terraform/` added to `.dockerignore` — the downloaded AWS provider plugin binaries
  (**648 MB**) were being copied into every image by `COPY . .`.
- The `chown -R` over `/app` and `/opt/hf` was removed. `chown -R` rewrites every file it
  touches into a fresh layer, adding a second full copy of both trees. Only the two
  genuinely runtime-writable paths are chowned now.

Before those fixes the multi-stage image measured **3.68 GB**.

## What this would cost on real AWS

Estimated from us-east-1 on-demand list prices as of **2026-09-07**, for the exact
topology in `terraform/` (2 api + 2 worker + 1 beat `t3.small`, RDS `db.t3.small`, S3,
CloudWatch). **This is an unvalidated estimate. It has never been checked against a bill,
and nobody should treat it as one.**

| Line item | Quantity | Est. monthly |
|---|---|---|
| EC2 `t3.small` on-demand | 5 × ~$15.18 | ~$75.90 |
| EBS gp3 root volumes | 5 × 8 GB | ~$3.20 |
| RDS `db.t3.small` single-AZ (`enable_rds=true`) | 1 | ~$24.82 |
| RDS gp3 storage | 20 GB | ~$2.30 |
| RDS backup storage (7-day retention) | ~20 GB | ~$1.90 |
| S3 standard (model cache + backups) | ~2 GB | ~$0.05 |
| CloudWatch Logs ingest + 14-day retention | ~1 GB | ~$0.53 |
| **Total** | | **~$109/month** (~$3.60/day) |

Not included, and each capable of moving the total: NAT gateway (~$32/month plus data
processing — `terraform/network.tf` deliberately has no NAT, so private-subnet workers
currently have no egress at all), an ALB (~$16/month plus LCU), data transfer out, and
ECR storage.

Deliberately excluded from the topology, and worth stating: there is **no ElastiCache**.
Redis has no cloud representation in `terraform/` at all, so the Terraform as written
cannot actually run the application end to end even with `enable_rds=true`.

### If it were run as a temporary exercise

At ~$3.60/day, bringing the stack up for a day of measurement and drills and then running
`scripts/teardown.sh` would cost roughly **$4**, plus a few cents of S3 and CloudWatch.
The teardown path exists precisely so that number stays a day's cost rather than a
month's.

## Reclaiming the local cost

```bash
bash scripts/teardown.sh --dry-run   # see what would go
bash scripts/teardown.sh --yes       # compose stacks, Terraform, LocalStack, images
docker builder prune                 # the 5.35 GB build cache is not touched by teardown
```

`scripts/teardown.sh` deliberately leaves the git repository, `evals/`, `docs/` and the
incident write-ups in place. Those are the output of the exercise, not part of the
running system.
