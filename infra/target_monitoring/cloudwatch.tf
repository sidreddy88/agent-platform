# Metric filter scans the target app's log group for Node.js error
# signatures. Pattern is intentionally Node-specific — different from
# the Python agent-platform self-monitor's `ERROR:` prefix:
#
#   - TypeError / ReferenceError / RangeError / SyntaxError — Node error
#     class names that prefix the error message
#   - UnhandledPromiseRejection — async errors escaping try/catch
#   - "Error: " (trailing space) — generic `throw new Error(...)` while
#     reducing false positives from messages that just contain the word
#     "error" or "Error" in passing
#
# ? prefix in CloudWatch filter syntax = OR.

resource "aws_cloudwatch_log_metric_filter" "errors" {
  name           = "${var.name_prefix}-errors"
  log_group_name = var.log_group_name

  pattern = "?\"TypeError:\" ?\"ReferenceError:\" ?\"RangeError:\" ?\"SyntaxError:\" ?\"UnhandledPromiseRejection\" ?\"Error: \""

  metric_transformation {
    name          = "${var.name_prefix}-error-count"
    namespace     = var.cloudwatch_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name          = "${var.name_prefix}-errors"
  alarm_description   = "Target app's ECS task emitted a Node error signature (TypeError/ReferenceError/RangeError/SyntaxError/UnhandledPromiseRejection/Error:)."
  metric_name         = aws_cloudwatch_log_metric_filter.errors.metric_transformation[0].name
  namespace           = aws_cloudwatch_log_metric_filter.errors.metric_transformation[0].namespace
  statistic           = "Sum"
  period              = var.error_period_seconds
  evaluation_periods  = 1
  threshold           = var.error_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]

  tags = {
    target = trimprefix(var.log_group_name, "/")
  }
}
