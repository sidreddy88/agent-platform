# Two IAM roles for the ECS task:
#
# 1. Task execution role — used by the ECS *agent* on the host to:
#    - Pull the image from ECR
#    - Write container logs to CloudWatch
#    - Read SSM SecureStrings to inject as env vars
#
# 2. Task role — used by the *application code* inside the container.
#    The agent platform itself reads CloudWatch logs (incident scanning),
#    publishes to SNS, describes ECS/EC2 (DeploymentAgent), etc.

# ---------------------------------------------------------------------------
# Task execution role
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task_execution" {
  name               = "${local.name}-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

# ECR pull + CloudWatch Logs write — AWS-managed policy.
resource "aws_iam_role_policy_attachment" "task_execution_managed" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Read every SSM parameter under /agent-platform/* — covers the secrets
# referenced from the task definition.
data "aws_iam_policy_document" "ssm_read" {
  statement {
    sid    = "ReadAgentPlatformParams"
    effect = "Allow"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
    ]
    resources = [
      "arn:aws:ssm:${var.region}:${local.account_id}:parameter/agent-platform/*",
    ]
  }

  statement {
    sid    = "DecryptSecureStrings"
    effect = "Allow"
    actions   = ["kms:Decrypt"]
    resources = ["arn:aws:kms:${var.region}:${local.account_id}:alias/aws/ssm"]
  }
}

resource "aws_iam_policy" "ssm_read" {
  name   = "${local.name}-ssm-read"
  policy = data.aws_iam_policy_document.ssm_read.json
}

resource "aws_iam_role_policy_attachment" "task_execution_ssm" {
  role       = aws_iam_role.task_execution.name
  policy_arn = aws_iam_policy.ssm_read.arn
}

# ---------------------------------------------------------------------------
# Task role (application runtime)
# ---------------------------------------------------------------------------

resource "aws_iam_role" "task_runtime" {
  name               = "${local.name}-task-runtime"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "task_runtime" {
  # CloudWatch Logs: read application logs for incident scanning,
  # write its own logs.
  statement {
    sid    = "CloudWatchLogsRead"
    effect = "Allow"
    actions = [
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
      "logs:GetLogEvents",
      "logs:FilterLogEvents",
      "logs:StartQuery",
      "logs:GetQueryResults",
    ]
    resources = ["*"]
  }

  # ECS / EC2 read for DeploymentAgent + monitoring.
  statement {
    sid    = "EcsRead"
    effect = "Allow"
    actions = [
      "ecs:Describe*",
      "ecs:List*",
      "ec2:Describe*",
      "elasticloadbalancing:Describe*",
      "cloudwatch:GetMetricData",
      "cloudwatch:GetMetricStatistics",
      "cloudwatch:DescribeAlarms",
      "cloudwatch:ListMetrics",
    ]
    resources = ["*"]
  }

  # SNS — confirm subscriptions and (later) publish from internal flows.
  statement {
    sid    = "SnsAccess"
    effect = "Allow"
    actions = [
      "sns:ConfirmSubscription",
      "sns:GetTopicAttributes",
      "sns:ListSubscriptionsByTopic",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "task_runtime" {
  name   = "${local.name}-task-runtime"
  policy = data.aws_iam_policy_document.task_runtime.json
}

resource "aws_iam_role_policy_attachment" "task_runtime" {
  role       = aws_iam_role.task_runtime.name
  policy_arn = aws_iam_policy.task_runtime.arn
}
