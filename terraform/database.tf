# count = var.enable_rds ? 1 : 0 — RDS is a LocalStack Pro feature
# (confirmed directly against this environment's LocalStack Community
# instance: `aws rds describe-db-instances` returns "API for service
# 'rds' not yet implemented or pro feature"). This resource is correct,
# real Terraform for an actual AWS account, and follows the same
# pgvector/pg15 shape as docker-compose.yml's postgres service and
# k8s/10-postgres.yaml's StatefulSet — it's written for when this
# deploys somewhere that can run it, not left out for being untestable
# here. `terraform plan` with enable_rds=true will show a coherent,
# reviewable resource; `terraform apply` against LocalStack Community
# will not attempt it unless you flip the variable.
resource "aws_db_subnet_group" "postgres" {
  count      = var.enable_rds ? 1 : 0
  name       = "${local.name}-postgres"
  subnet_ids = aws_subnet.private[*].id
  tags       = { Name = "${local.name}-postgres" }
}

resource "aws_db_instance" "postgres" {
  count      = var.enable_rds ? 1 : 0
  identifier = "${local.name}-postgres"

  # RDS's own Postgres engine does not bundle pgvector as an available
  # extension until relatively recent RDS Postgres versions (15.2+ /
  # 14.7+) — pinned to 15 to match docker-compose.yml/k8s/10-postgres.yaml.
  engine         = "postgres"
  engine_version = "15"
  instance_class = "db.t3.small"

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = "talentscope"
  username = "talentscope"
  password = var.db_password

  db_subnet_group_name   = aws_db_subnet_group.postgres[0].name
  vpc_security_group_ids = [aws_security_group.db.id]

  # Single-AZ by default (multi_az below is the one-line flip for
  # production) — matches this being a demo/dev-shaped deployment, same
  # as postgres running as a single StatefulSet replica in k8s/10-postgres.yaml
  # rather than a HA pair.
  multi_az = false

  backup_retention_period = 7
  skip_final_snapshot     = true # dev/demo default — false + a final_snapshot_identifier for anything real

  tags = { Name = "${local.name}-postgres" }
}
