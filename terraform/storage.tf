# Holds the sentence-transformers model cache (app/search/encoder.py — the
# same ~/.cache/huggingface content docker-compose.yml keeps in the hf_cache
# named volume) so a fresh instance doesn't re-download the model from
# HuggingFace on every launch — a real cold-start latency and egress-cost
# concern once this runs on more than one host.
resource "aws_s3_bucket" "model_cache" {
  bucket = "${local.name}-model-cache"
  tags   = { Name = "${local.name}-model-cache" }
}

resource "aws_s3_bucket_versioning" "model_cache" {
  bucket = aws_s3_bucket.model_cache.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_public_access_block" "model_cache" {
  bucket                  = aws_s3_bucket.model_cache.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "model_cache" {
  bucket = aws_s3_bucket.model_cache.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# ---------------------------------------------------------------------------
# Database backups
# ---------------------------------------------------------------------------
# Separate bucket from the model cache on purpose: different data, different
# lifecycle, and different blast radius if the policy is ever wrong. The model
# cache is reproducible from HuggingFace; these are the only copy of the data.
#
# Written to by scripts/backup_db.sh (pg_dump -Fc | gzip) and read by
# scripts/restore_db.sh. Both work against LocalStack S3, which implements
# enough of the API for this to be genuinely exercised rather than described —
# see docs/incidents/ for the restore drill that used it.
resource "aws_s3_bucket" "backups" {
  bucket = "${local.name}-backups"
  tags   = { Name = "${local.name}-backups" }
}

# Versioning matters more here than on the model cache: it is the difference
# between "someone overwrote last night's dump with a corrupt one" being an
# inconvenience and being the end of the recovery story.
resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# count = var.enable_s3_lifecycle ? 1 : 0 — same idiom as database.tf's
# enable_rds, and for a directly-confirmed reason. Against LocalStack
# Community 3.0 the rule *is* created — `aws s3api
# get-bucket-lifecycle-configuration --bucket talentscope-dev-backups` returns
# it in full, expiration, noncurrent-version expiry and all — but the AWS
# provider's post-create consistency waiter never observes it converge and
# fails after its 3-minute timeout, twice in a row from a clean apply. So this
# is correct Terraform that a real account will apply; it is left off by
# default only because LocalStack cannot confirm it back to the provider.
# `terraform plan` with enable_s3_lifecycle=true shows a coherent resource.
resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  count  = var.enable_s3_lifecycle ? 1 : 0
  bucket = aws_s3_bucket.backups.id

  rule {
    id     = "expire-old-backups"
    status = "Enabled"

    filter {} # whole bucket

    # Retention deliberately matches the RDS backup_retention_period of 7 days
    # in database.tf, so the two recovery paths cover the same window rather
    # than one quietly expiring first.
    expiration {
      days = var.backup_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = var.backup_retention_days
    }

    # Multipart uploads from an interrupted large dump are invisible in a
    # bucket listing but still billed.
    abort_incomplete_multipart_upload {
      days_after_initiation = 3
    }
  }
}
