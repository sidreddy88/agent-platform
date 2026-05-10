# AllInterviews error monitoring

Push-based error monitoring for the AllInterviews production ECS service. Watches `/ecs/TaskAllInterviews` for Node.js error signatures, fires CloudWatch alarms, and pushes them to the agent-platform webhook at `https://app.remediatelabs.io/webhooks/cloudwatch-alarm`.

**Why a separate Terraform module:** AllInterviews lives in AWS account `950252867672` (us-east-2). The main `agent_platform` module targets account `542337758768` (us-east-1). State and credentials are cleanly separated so neither account ever needs to know about the other.

## What it provisions

| Resource | What it does |
|---|---|
| `aws_sns_topic.alarms` | SNS topic that CloudWatch alarms publish to |
| `aws_sns_topic_subscription.webhook` | HTTPS subscription POSTing to `/webhooks/cloudwatch-alarm` (auto-confirms via the webhook handler) |
| `aws_cloudwatch_log_metric_filter.errors` | Pattern match on Node error signatures in `/ecs/TaskAllInterviews` |
| `aws_cloudwatch_metric_alarm.errors` | Fires on any matched error in a 60s window (tunable) |

## Apply

```bash
cd infra/allinterviews_monitoring

# Pull the webhook token from agent-platform's SSM (uses the
# agent-platform AWS profile, not the AllInterviews one)
export TF_VAR_webhook_token=$(
  aws ssm get-parameter \
    --profile agent-platform \
    --name /agent-platform/CLOUDWATCH_WEBHOOK_TOKEN \
    --with-decryption \
    --query Parameter.Value \
    --output text
)

# Switch to AllInterviews creds (admin-scoped — needs PutMetricFilter,
# cloudwatch:PutMetricAlarm, sns:CreateTopic, sns:Subscribe).
# AgentPlatformMonitor is read-only and will fail.
export AWS_PROFILE=allinterviews-admin   # or whatever profile name you use

terraform init
terraform plan      # review
terraform apply     # 4 resources added
```

## Verify

```bash
# Subscription should show a real ARN, not "PendingConfirmation"
aws sns list-subscriptions-by-topic \
  --topic-arn $(terraform output -raw sns_topic_arn)

# Trigger a synthetic test to confirm end-to-end push
aws sns publish \
  --topic-arn $(terraform output -raw sns_topic_arn) \
  --subject "ALARM: synthetic-test" \
  --message '{"AlarmName":"synthetic-test","NewStateValue":"ALARM","Region":"us-east-2","AlarmArn":"arn:aws:cloudwatch:us-east-2:950252867672:alarm:synthetic-test"}'
```

Then open `app.remediatelabs.io` → Events tab → a new pending event should appear.

## Tear down

```bash
terraform destroy
```

## Tuning

Threshold / period are variables — defaults are `Sum > 0 in 60s`, meaning any single error fires. The platform's dedup gate collapses identical errors into one incident, but if the alarm starts flooding the dashboard, raise:

- `TF_VAR_error_threshold=4` → require 5+ errors per period
- `TF_VAR_error_period_seconds=300` → 5-minute window instead of 60s

## Permissions needed

The AWS profile used to apply needs (at minimum) these actions in account `950252867672`:

| Service | Actions |
|---|---|
| `logs` | `PutMetricFilter`, `DeleteMetricFilter`, `DescribeMetricFilters` |
| `cloudwatch` | `PutMetricAlarm`, `DeleteAlarms`, `DescribeAlarms` |
| `sns` | `CreateTopic`, `DeleteTopic`, `Subscribe`, `Unsubscribe`, `GetTopicAttributes`, `SetTopicAttributes`, `ListSubscriptionsByTopic` |

`AdministratorAccess` is the simplest grant; a scoped policy works too.

## What this does NOT change

- Zero load on the AllInterviews ECS task: the `awslogs` driver was already shipping stdout/stderr; metric filter + alarm evaluation run on AWS-managed compute
- Zero changes to AllInterviews application code
- Zero changes to the agent-platform deployment
