output "sns_topic_arn" {
  description = "SNS topic CloudWatch alarms publish to in the target app's account."
  value       = aws_sns_topic.alarms.arn
}

output "alarm_arn" {
  description = "CloudWatch alarm watching the target app's log group for error signatures."
  value       = aws_cloudwatch_metric_alarm.errors.arn
}

output "metric_filter_name" {
  description = "Metric filter name. Useful for `aws logs delete-metric-filter` if you ever need to manually tear down."
  value       = aws_cloudwatch_log_metric_filter.errors.name
}
