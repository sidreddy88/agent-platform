# SNS topic in the target app's account. CloudWatch alarms publish to
# same-region same-account SNS topics by default, so the topic lives
# here rather than in the agent-platform account.
#
# The HTTPS subscription POSTs to the agent-platform webhook running at
# app.remediatelabs.io. The shared-secret token in the query string is
# the auth boundary — Cloudflare Access has a Bypass policy on the
# /webhooks/* path so SNS can reach the origin without a login wall.

resource "aws_sns_topic" "alarms" {
  name = "${var.name_prefix}-alarms"

  tags = {
    purpose = "cloudwatch-alarm-fanout-to-webhook"
  }
}

locals {
  webhook_url = sensitive(
    "${var.webhook_url_base}/webhooks/cloudwatch-alarm?token=${var.webhook_token}"
  )
}

resource "aws_sns_topic_subscription" "webhook" {
  topic_arn              = aws_sns_topic.alarms.arn
  protocol               = "https"
  endpoint               = local.webhook_url
  endpoint_auto_confirms = true  # webhook handler auto-confirms on first POST
  raw_message_delivery   = false # keep SNS envelope for TopicArn/MessageId
}
