# TalentScope on AWS: Terraform + LocalStack

Applied and verified against a real LocalStack instance — `terraform apply`
ran, every resource was confirmed via the AWS CLI afterward (not just
"terraform said success"), and one real bug was hit and fixed along the way.
Not a set of `.tf` files written and left untested.

## Setup

LocalStack's CLI (2026.8.0, installed via `brew install localstack/tap/localstack-cli`)
now hard-requires an account/auth token even for the community tier —
confirmed directly (`localstack start` fails with "LocalStack requires an
account to run" even with the documented `LOCALSTACK_ACKNOWLEDGE_ACCOUNT_REQUIREMENT=1`
grace-period bypass). Rather than requiring an account signup for a local
proof-of-concept, this runs the community image directly, pinned to a
version from before that requirement existed:

```bash
docker run -d --name talentscope-localstack -p 4566:4566 localstack/localstack:3.0
```

```bash
cd terraform
terraform init
cp terraform.tfvars.example terraform.tfvars
terraform plan -out=tfplan
terraform apply tfplan
```

## What's real vs. what's LocalStack Pro

Checked directly against this LocalStack Community instance
(`aws --endpoint-url=http://localhost:4566 <service> <call>`), not assumed
from documentation, since LocalStack's free/paid service boundary has
shifted over time and secondhand claims about it age badly:

| Service | Status | Evidence |
|---|---|---|
| EC2 (incl. VPC/subnets/SGs) | ✅ Community | `ec2:*` calls succeed |
| IAM | ✅ Community | role/instance-profile created and read back |
| S3 | ✅ Community | bucket created, listed |
| CloudWatch (metrics + logs) | ✅ Community | `PutMetricAlarm` → 200 in LocalStack's own request log |
| Secrets Manager | ✅ Community | secret created, value read back |
| **RDS** | ❌ Pro only | `DescribeDBInstances` → `"API for service 'rds' not yet implemented or pro feature"` |
| **ECS** | ❌ Pro only | `ListClusters` → same error |
| **Auto Scaling (ASG)** | ❌ Pro only | `DescribeAutoScalingGroups` → same error |
| **ELBv2** | ❌ Pro only | `DescribeLoadBalancers` → same error |

This shaped the architecture directly rather than being a footnote:

- **Compute is plain `aws_instance`, not an Auto Scaling Group** (`compute.tf`). A real deployment on this same network/IAM/security-group foundation would put the api tier behind an ALB + target group with a target-tracking scaling policy on CPU — the direct AWS-native equivalent of `k8s/20-api.yaml`'s `HorizontalPodAutoscaler`. Not written as inert, unappliable Terraform against APIs that don't exist here; documented as the concrete next step instead, once this targets a real account.
- **RDS is written, not applied** (`database.tf`, gated behind `var.enable_rds`, default `false`). It's correct Terraform for a real AWS target — pgvector-capable Postgres 15, matching `docker-compose.yml`'s `postgres` service and `k8s/10-postgres.yaml`'s StatefulSet — but `terraform plan -var enable_rds=true` is as far as this environment can verify it. `terraform apply` against LocalStack Community will not attempt it unless the flag is flipped.
- **CloudWatch alarms attach to individual instances**, not an ASG's aggregate metrics, for the same reason.

## Architecture

```
VPC (10.20.0.0/16)
├── 2 public subnets  → api tier (2x EC2, internet-facing)
├── 2 private subnets → worker tier (2x EC2) + beat (1x EC2, singleton — no leader election, same reasoning as k8s/22-beat.yaml)
├── security groups: api (0.0.0.0/0:8000) → worker (VPC-internal:9808) → db (api/worker SGs only:5432)
├── IAM role: least-privilege — read own Secrets Manager secret, read/write model-cache S3 bucket, write own CloudWatch log group
├── S3: talentscope-dev-model-cache (versioned, encrypted, public access blocked) — the sentence-transformers cache, same content as docker-compose.yml's hf_cache volume
├── Secrets Manager: talentscope-dev/app (GROQ_API_KEY, DB_PASSWORD when RDS is on)
└── CloudWatch: log group + per-instance StatusCheckFailed/CPUUtilization alarms
```

## What broke while validating this (and the fix)

**Every S3 call failed with `Unable to find operation for request to
service s3: HEAD /`**, retrying with growing backoff (Terraform's own
retry logic making it look like a hang rather than a failure — the apply
sat for several minutes before this was diagnosed from LocalStack's
container logs, not Terraform's own output). Root cause: the AWS provider
defaults to virtual-hosted-style S3 addressing
(`bucket-name.s3.amazonaws.com`), which LocalStack's single edge endpoint
(`localhost:4566`) can't route — there's no bucket name in the hostname to
dispatch on. Fixed with one line in `providers.tf`:
`s3_use_path_style = var.use_localstack`. Harmless against real AWS, which
supports both addressing styles.

## Verified resources (post-apply, via AWS CLI — not just `terraform state list`)

- 5 EC2 instances, correctly placed: 2 api in public subnets, 2 worker + 1 beat in private subnets
- 1 S3 bucket (`talentscope-dev-model-cache`), listed via `aws s3 ls`
- 1 IAM role + instance profile, read back via `aws iam get-role`
- 1 Secrets Manager secret, value read back via `aws secretsmanager get-secret-value`
- 4 CloudWatch alarms (2 instances × {StatusCheckFailed, CPUUtilization}), confirmed via LocalStack's own request log (`PutMetricAlarm => 200`) — the AWS CLI's own `describe-alarms` against this LocalStack/CLI version combination hit an unrelated protocol-detection error, a CLI-tooling quirk rather than a Terraform or resource problem

38 resources total in state (`terraform state list | wc -l`).

## Not attempted here

- **`terraform destroy` idempotency** — not exercised in this pass; worth confirming before treating this as a template for repeated apply/destroy cycles.
- **Multi-environment state** (a remote backend, workspaces) — this uses local state (`terraform/*.tfstate`, gitignored), fine for one person proving out a design, not for a team.
- **RDS/ECS/ASG/ELB applied for real** — would need either a LocalStack Pro token or a real AWS account; the Terraform for RDS is written and `terraform plan`-verified, the rest (ASG/ELB) is documented as the next step rather than written unappliable.
