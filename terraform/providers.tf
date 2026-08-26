# Targets LocalStack by default (var.use_localstack = true) so this can be
# applied without real AWS credentials or cost. Every endpoint override
# below is a no-op against real AWS — flip use_localstack to false (and
# supply real credentials) to point this same configuration at an actual
# account. That's the point of writing it this way: the Terraform itself
# doesn't change between "local proof" and "the real thing," only where it
# points.
provider "aws" {
  region                      = var.aws_region
  access_key                  = var.use_localstack ? "test" : null
  secret_key                  = var.use_localstack ? "test" : null
  skip_credentials_validation = var.use_localstack
  skip_metadata_api_check     = var.use_localstack
  skip_requesting_account_id  = var.use_localstack

  # Found by actually applying this against LocalStack, not assumed: the
  # AWS provider defaults to virtual-hosted-style S3 addressing
  # (bucket.s3.amazonaws.com), which LocalStack's single edge endpoint
  # can't route ("Unable to find operation for request to service s3:
  # HEAD /") — every S3 call retried with growing backoff until this was
  # set. Real AWS supports both styles, so this is harmless there too.
  s3_use_path_style = var.use_localstack

  dynamic "endpoints" {
    for_each = var.use_localstack ? [1] : []
    content {
      ec2            = var.localstack_endpoint
      iam            = var.localstack_endpoint
      s3             = var.localstack_endpoint
      sts            = var.localstack_endpoint
      cloudwatch     = var.localstack_endpoint
      cloudwatchlogs = var.localstack_endpoint
      secretsmanager = var.localstack_endpoint
      rds            = var.localstack_endpoint
      ecs            = var.localstack_endpoint
      elbv2          = var.localstack_endpoint
    }
  }
}
