variable "use_localstack" {
  description = "Route every AWS API call through LocalStack instead of real AWS. Default true — this repo is meant to be applied against LocalStack for free, reviewable IaC evidence, not a live account."
  type        = bool
  default     = true
}

variable "localstack_endpoint" {
  description = "LocalStack's single edge endpoint (all services multiplex through it)."
  type        = string
  default     = "http://localhost:4566"
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "talentscope"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "az_count" {
  description = "Number of availability zones to spread subnets across."
  type        = number
  default     = 2
}

variable "instance_type" {
  description = "EC2 instance type for the api/worker hosts. LocalStack Community doesn't run real compute (see database.tf's header comment for the same caveat on RDS) — this drives the *shape* of the request, which is what's actually being validated here."
  type        = string
  default     = "t3.small"
}

variable "enable_rds" {
  description = <<-EOT
    RDS is a LocalStack Pro feature (confirmed directly: `aws rds
    describe-db-instances` against LocalStack Community returns "API for
    service 'rds' not yet implemented or pro feature"). database.tf's
    resources are written for a real AWS target and are correct Terraform,
    but `terraform apply` against LocalStack Community will fail on them
    unless this is left false. Flip to true only when use_localstack is
    also false (a real account) or you have a LocalStack Pro token.
  EOT
  type        = bool
  default     = false
}

variable "db_password" {
  description = "Only read when enable_rds = true. Prefer TF_VAR_db_password or a real secrets backend over a .tfvars file — never commit an actual value."
  type        = string
  default     = "changeme-not-a-real-password"
  sensitive   = true
}

variable "groq_api_key" {
  description = "Stored in Secrets Manager (secrets.tf), not baked into any instance's user_data or a ConfigMap-equivalent — mirrors the k8s/ phase's ConfigMap/Secret split at the AWS layer."
  type        = string
  default     = "placeholder-set-via-TF_VAR_groq_api_key"
  sensitive   = true
}
