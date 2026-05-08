#!/usr/bin/env bash
# Bash equivalent of cloudwatch_sns_subscription.tf — for users who'd
# rather wire this up with the AWS CLI than apply Terraform.
#
# Sets up:
#   1. SNS topic `agent-platform-alarms`
#   2. HTTPS subscription pointing at the agent-platform webhook
#
# After running this, set the printed ARN as
# CLOUDWATCH_ALARM_SNS_TOPIC_ARN in the agent-platform .env, then
# re-run MonitorGenerationAgent so its alarms get the new AlarmActions.
#
# Usage:
#   AWS_REGION=us-east-1 \
#   WEBHOOK_URL='https://agent-platform.example.com/webhooks/cloudwatch-alarm?token=YOUR_TOKEN' \
#   ./setup_cloudwatch_sns.sh

set -euo pipefail

: "${AWS_REGION:?AWS_REGION must be set}"
: "${WEBHOOK_URL:?WEBHOOK_URL must be set (https://.../webhooks/cloudwatch-alarm?token=...)}"

TOPIC_NAME="agent-platform-alarms"

echo "→ Creating SNS topic '${TOPIC_NAME}' in ${AWS_REGION}..."
TOPIC_ARN=$(aws sns create-topic \
  --name "${TOPIC_NAME}" \
  --region "${AWS_REGION}" \
  --query TopicArn \
  --output text)
echo "  topic ARN: ${TOPIC_ARN}"

echo "→ Subscribing webhook to topic..."
aws sns subscribe \
  --topic-arn "${TOPIC_ARN}" \
  --protocol https \
  --notification-endpoint "${WEBHOOK_URL}" \
  --region "${AWS_REGION}" \
  --output text > /dev/null

echo
echo "Done. Add to your agent-platform .env:"
echo "  CLOUDWATCH_ALARM_SNS_TOPIC_ARN=${TOPIC_ARN}"
echo
echo "The platform will auto-confirm the SNS subscription on first POST."
echo "Existing CloudWatch alarms can be wired in via:"
echo "  aws cloudwatch put-metric-alarm --alarm-actions ${TOPIC_ARN} ..."
echo "or by re-running MonitorGenerationAgent on a merged PR with"
echo "CREATE_MONITORS=true."
