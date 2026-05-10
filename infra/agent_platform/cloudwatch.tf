# Container log group. ECS streams every line of stdout/stderr from the
# task here.
#
# 30-day retention is plenty for incident scanning + interview demos
# without piling up storage cost.

resource "aws_cloudwatch_log_group" "agent_platform" {
  name              = "/ecs/${local.name}"
  retention_in_days = 30
}

# ---------------------------------------------------------------------------
# Self-monitoring alarm
# ---------------------------------------------------------------------------
#
# Metric filter scans the platform's own ECS log stream for genuine error
# signatures — Python ERROR/CRITICAL log lines or unhandled tracebacks —
# and increments a CloudWatch metric. The alarm fires on any non-zero
# count in a 60s window, publishing to the SNS topic that the
# /webhooks/cloudwatch-alarm handler subscribes to.
#
# This closes the loop: the agent platform watches itself. Any genuine
# error in its own runtime becomes a pending event in its own dashboard.
# Useful as a live interview demo of the end-to-end pipeline and as a
# canary for the agent platform's own reliability.

resource "aws_cloudwatch_log_metric_filter" "platform_errors" {
  name           = "${local.name}-errors"
  log_group_name = aws_cloudwatch_log_group.agent_platform.name

  # CloudWatch filter pattern: any of these substrings triggers the metric.
  # ? prefix = OR. Matches Python stdlib log prefixes and traceback markers.
  pattern = "?\"ERROR:\" ?\"CRITICAL:\" ?\"Traceback (most recent call last)\""

  metric_transformation {
    name          = "${local.name}-error-count"
    namespace     = "AgentPlatform"
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "platform_errors" {
  alarm_name          = "${local.name}-errors"
  alarm_description   = "Agent platform task emitted an ERROR/CRITICAL log line or unhandled exception."
  metric_name         = aws_cloudwatch_log_metric_filter.platform_errors.metric_transformation[0].name
  namespace           = aws_cloudwatch_log_metric_filter.platform_errors.metric_transformation[0].namespace
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]

  tags = {
    purpose = "self-monitoring"
  }
}
