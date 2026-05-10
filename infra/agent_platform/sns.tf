# CloudWatch alarm fan-out to the agent-platform webhook.
#
# CloudWatch alarms (created by MonitorGenerationAgent or manually wired
# via `aws cloudwatch put-metric-alarm --alarm-actions ...`) publish to
# this SNS topic. SNS POSTs each notification to /webhooks/cloudwatch-alarm
# on the running ECS task. The webhook handler auto-confirms the
# SubscriptionConfirmation on first POST.
#
# This is the push-based ingest pipeline: production has zero polling
# load — AWS does the watching and only signals when an alarm fires.

resource "aws_sns_topic" "alarms" {
  name = "${local.name}-alarms"

  tags = {
    purpose = "cloudwatch-alarm-fanout-to-webhook"
  }
}

# The webhook handler validates the `token=` query param against the
# CLOUDWATCH_WEBHOOK_TOKEN env var (read from SSM at task start).
# Building the subscription URL inline so the same SSM value drives
# both the SNS subscription and the running container.
data "aws_ssm_parameter" "cloudwatch_webhook_token" {
  name            = aws_ssm_parameter.secret["CLOUDWATCH_WEBHOOK_TOKEN"].name
  with_decryption = true
}

locals {
  # Marked sensitive so plan output doesn't leak the token.
  webhook_url = sensitive(
    "https://${var.hostname}/webhooks/cloudwatch-alarm?token=${data.aws_ssm_parameter.cloudwatch_webhook_token.value}"
  )
}

resource "aws_sns_topic_subscription" "webhook" {
  topic_arn              = aws_sns_topic.alarms.arn
  protocol               = "https"
  endpoint               = local.webhook_url
  endpoint_auto_confirms = true  # FastAPI handler auto-confirms on first POST
  raw_message_delivery   = false # keep the SNS envelope (TopicArn, MessageId, etc.)
}
