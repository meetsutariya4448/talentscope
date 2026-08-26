# EC2 instance role: least-privilege for what a running api/worker host
# actually needs — write its own logs, read the one secret it needs, read
# the model-cache bucket. No wildcard resource ARNs.

data "aws_iam_policy_document" "ec2_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "app" {
  name               = "${local.name}-app"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json
}

data "aws_iam_policy_document" "app_permissions" {
  statement {
    sid       = "ReadOwnSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.app.arn]
  }

  statement {
    sid = "ModelCacheBucket"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.model_cache.arn,
      "${aws_s3_bucket.model_cache.arn}/*",
    ]
  }

  statement {
    sid = "OwnLogGroup"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = ["${aws_cloudwatch_log_group.app.arn}:*"]
  }
}

resource "aws_iam_role_policy" "app" {
  name   = "${local.name}-app-permissions"
  role   = aws_iam_role.app.id
  policy = data.aws_iam_policy_document.app_permissions.json
}

resource "aws_iam_instance_profile" "app" {
  name = "${local.name}-app"
  role = aws_iam_role.app.name
}
