# Same split as the Kubernetes phase (k8s/01-configmap.yaml vs.
# 02-secret.example.yaml, and k8s/apply.sh's comment on where an External
# Secrets Operator would sit): non-secret config lives in the launch
# template's user_data / environment, credentials live here, read by the
# instance role at runtime via secretsmanager:GetSecretValue — never baked
# into an AMI, launch template, or committed file.
resource "aws_secretsmanager_secret" "app" {
  name        = "${local.name}/app"
  description = "TalentScope app secrets — GROQ_API_KEY and (when enable_rds is on) the DB password."
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({
    GROQ_API_KEY = var.groq_api_key
    DB_PASSWORD  = var.enable_rds ? var.db_password : null
  })
}
