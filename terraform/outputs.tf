output "vpc_id" {
  value = aws_vpc.main.id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "api_instance_ids" {
  value = aws_instance.api[*].id
}

output "worker_instance_ids" {
  value = aws_instance.worker[*].id
}

output "beat_instance_id" {
  value = aws_instance.beat[0].id
}

output "model_cache_bucket" {
  value = aws_s3_bucket.model_cache.bucket
}

output "app_secret_arn" {
  value = aws_secretsmanager_secret.app.arn
}

output "db_endpoint" {
  value       = var.enable_rds ? aws_db_instance.postgres[0].endpoint : "enable_rds=false — no RDS instance provisioned, see database.tf"
  description = "Only meaningful when enable_rds = true."
}
