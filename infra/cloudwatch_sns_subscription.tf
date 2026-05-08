# CloudWatch alarm → SNS → agent-platform webhook
#
# Creates the SNS topic that auto-generated CloudWatch alarms publish to,
# and subscribes the agent-platform's HTTPS webhook to it. Once applied,
# every alarm transition (state → ALARM) becomes an HTTP POST to the
# platform — no polling on the production server.
#
# The platform's MonitorGenerationAgent will inject this topic ARN into
# the AlarmActions of every alarm it provisions when CREATE_MONITORS=true
# and CLOUDWATCH_ALARM_SNS_TOPIC_ARN is set in the platform's .env.
#
# Apply:
#   terraform init
#   terraform apply
#
# Destroy:
#   terraform destroy

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "aws_region" {
  default = "us-east-1"
}

variable "webhook_url" {
  description = "Public HTTPS URL of the agent-platform CloudWatch webhook"
  # Example: "https://agent-platform.example.com/webhooks/cloudwatch-alarm?token=YOUR_TOKEN"
  type        = string
}

provider "aws" {
  region = var.aws_region
}

resource "aws_sns_topic" "agent_platform_alarms" {
  name = "agent-platform-alarms"
  tags = {
    project = "agent-platform"
    purpose = "cloudwatch-alarm-fanout-to-webhook"
  }
}

resource "aws_sns_topic_subscription" "agent_platform_webhook" {
  topic_arn              = aws_sns_topic.agent_platform_alarms.arn
  protocol               = "https"
  endpoint               = var.webhook_url
  endpoint_auto_confirms = true   # platform auto-confirms the SubscriptionConfirmation
  raw_message_delivery   = false  # keep SNS envelope so we get TopicArn / MessageId
}

output "sns_topic_arn" {
  value       = aws_sns_topic.agent_platform_alarms.arn
  description = "Set this as CLOUDWATCH_ALARM_SNS_TOPIC_ARN in agent-platform .env"
}
