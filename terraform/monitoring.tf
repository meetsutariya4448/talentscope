# CloudWatch here is deliberately narrow in scope: the Prometheus/Grafana
# stack (observability/, docs/observability.md) is the actual metrics
# story for this app — dashboards, PromQL, the multiprocess Celery
# aggregation. What CloudWatch adds on top, and is the right tool for, is
# infrastructure-level signal Prometheus can't see from inside the VPC:
# instance health checks and a durable log sink outside the instance
# itself (so a terminated host doesn't take its logs with it).

resource "aws_cloudwatch_log_group" "app" {
  name              = "/${var.project}/${var.environment}/app"
  retention_in_days = 14
}

# One alarm per api instance, not one on an ASG (see compute.tf's header —
# no aws_autoscaling_group here, since autoscaling is also a LocalStack Pro
# feature, confirmed the same way RDS/ELB were).
resource "aws_cloudwatch_metric_alarm" "api_status_check_failed" {
  for_each = { for i, inst in aws_instance.api : i => inst }

  alarm_name          = "${local.name}-api-${each.key}-status-check-failed"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "StatusCheckFailed"
  namespace           = "AWS/EC2"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "EC2 instance/system status check failed — the infra-level signal that sits below app-level health, which /health and /ready (app/main.py) already cover."
  dimensions = {
    InstanceId = each.value.id
  }
}

resource "aws_cloudwatch_metric_alarm" "api_cpu_high" {
  for_each = { for i, inst in aws_instance.api : i => inst }

  alarm_name          = "${local.name}-api-${each.key}-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "CPUUtilization"
  namespace           = "AWS/EC2"
  period              = 300
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "Sustained high CPU — a leading indicator worth paging on before /ready starts failing, not after. On real AWS this dimension would be an AutoScalingGroupName instead, driving a target-tracking scaling policy directly rather than just alerting (see compute.tf's header)."
  dimensions = {
    InstanceId = each.value.id
  }
}
