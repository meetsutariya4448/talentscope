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
