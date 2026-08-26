# Plain aws_instance, not an Auto Scaling Group — confirmed directly
# (`aws autoscaling describe-auto-scaling-groups` and `aws elbv2
# describe-load-balancers` both return "not yet implemented or pro
# feature" against LocalStack Community, the same wall RDS hit). A real
# deployment on this same network/IAM/security-group foundation would put
# the api tier behind an Application Load Balancer + target group and an
# aws_autoscaling_group with a target-tracking policy on CPU (the direct
# AWS-native equivalent of k8s/20-api.yaml's HorizontalPodAutoscaler) —
# not written here as inert, unappliable Terraform against an API that
# doesn't exist in this environment; documented as the concrete next step
# instead (docs/terraform.md).
#
# Instance counts below are the fixed equivalent of the Kubernetes phase's
# steady-state replica counts (k8s/20-api.yaml, k8s/21-worker.yaml): 2 api,
# 2 worker, 1 beat (never more than one — same reasoning as beat's
# `replicas: 1` there: no leader election, a second instance double-fires
# every scheduled job).

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }
}

locals {
  # Minimal cloud-init: install Docker, log in nowhere (image is public),
  # run the container with config pulled from Secrets Manager at boot
  # rather than baked into the AMI or this template. Illustrative, not
  # exercised against LocalStack — there's no real instance behind an
  # EC2 API call here to actually boot it.
  user_data = <<-EOF
    #!/bin/bash
    set -euxo pipefail
    dnf install -y docker
    systemctl enable --now docker
    SECRET=$(aws secretsmanager get-secret-value --secret-id ${aws_secretsmanager_secret.app.name} --query SecretString --output text --region ${var.aws_region})
    GROQ_API_KEY=$(echo "$SECRET" | python3 -c 'import json,sys; print(json.load(sys.stdin)["GROQ_API_KEY"])')
    docker run -d --name talentscope-$${role} \
      -e GROQ_API_KEY="$GROQ_API_KEY" \
      -e DATABASE_URL="${var.enable_rds ? "postgresql://talentscope:${var.db_password}@${try(aws_db_instance.postgres[0].address, "")}:5432/talentscope" : "postgresql://talentscope:talentscope@localhost:5432/talentscope"}" \
      talentscope:latest
  EOF
}

resource "aws_instance" "api" {
  count                  = 2
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public[count.index % var.az_count].id
  vpc_security_group_ids = [aws_security_group.api.id]
  iam_instance_profile   = aws_iam_instance_profile.app.name
  user_data              = replace(local.user_data, "$${role}", "api")

  tags = { Name = "${local.name}-api-${count.index}", Role = "api" }
}

resource "aws_instance" "worker" {
  count                  = 2
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.private[count.index % var.az_count].id
  vpc_security_group_ids = [aws_security_group.worker.id]
  iam_instance_profile   = aws_iam_instance_profile.app.name
  user_data              = replace(local.user_data, "$${role}", "worker")

  tags = { Name = "${local.name}-worker-${count.index}", Role = "worker" }
}

resource "aws_instance" "beat" {
  # MUST stay at 1 — same reasoning as k8s/22-beat.yaml: no leader
  # election, a second instance double-fires every scheduled task.
  count                  = 1
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.private[0].id
  vpc_security_group_ids = [aws_security_group.worker.id]
  iam_instance_profile   = aws_iam_instance_profile.app.name
  user_data              = replace(local.user_data, "$${role}", "beat")

  tags = { Name = "${local.name}-beat", Role = "beat" }
}
