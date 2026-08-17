# ECS Fargate cluster + service for the agent platform.
#
# Cluster is dedicated (separate from any existing AllInterviews cluster
# you run today) for blast-radius isolation: the agent platform's
# bursty LLM calls and chaos-test-shaped behaviour can't take down a
# production workload sharing the same cluster.

resource "aws_ecs_cluster" "agent_platform" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "fargate" {
  cluster_name       = aws_ecs_cluster.agent_platform.name
  capacity_providers = ["FARGATE"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
    base              = 1
  }
}

# ---------------------------------------------------------------------------
# Task definition
# ---------------------------------------------------------------------------

locals {
  # Environment variables that aren't secrets — injected directly.
  task_env = [
    { name = "PORT",                          value = tostring(var.container_port) },
    { name = "AWS_REGION",                    value = var.region },
    { name = "ENVIRONMENT",                   value = var.environment },
    { name = "PYTHONUNBUFFERED",              value = "1" },
    { name = "HARNESS_DOCS_PATH",             value = "targets/allinterviews" },
    # SNS topic the MonitorGenerationAgent wires into AlarmActions when
    # creating new CloudWatch alarms. Not a secret — just an ARN.
    { name = "CLOUDWATCH_ALARM_SNS_TOPIC_ARN", value = aws_sns_topic.alarms.arn },
    # DetectionService scans this log group every 5 min for AllInterviews
    # error lines and surfaces them in the dashboard for fix-agent triage.
    # ECS_LOG_GROUPS_REGION lets the boto3 client target a different region
    # than agent-platform's own infrastructure (us-east-1).
    { name = "ECS_LOG_GROUPS",        value = "/ecs/TaskAllInterviews" },
    { name = "ECS_LOG_GROUPS_REGION", value = "us-east-2" },
    # Target repo for AI-generated fix PRs. Previously carried only by
    # app/core/config.py's hardcoded default -- that default was changed to
    # a generic placeholder in PR #151 (scrubbing the real org/repo name out
    # of source ahead of the repo eventually going public), which silently
    # broke this in prod since nothing else was ever setting it here. Not a
    # secret -- this file already names the target app in plain text above
    # (ECS_LOG_GROUPS) -- so it's a plain env var, not routed through SSM.
    { name = "FIX_TARGET_REPO", value = "VoyageGroupMag/AllInterviews" },
    # Langfuse's generic host (settings.langfuse_host's default,
    # "https://cloud.langfuse.com") does not accept this project's keys --
    # this specific project lives on the US-region-specific ingest host.
    # This was never set here (confirmed: absent from both this file and
    # secrets.tf), so every span export from prod has been failing with a
    # 401 the entire time, on top of (and independent from) the
    # LANGFUSE_SECRET_KEY placeholder-value issue found the same session.
    # Not a secret -- just a URL.
    { name = "LANGFUSE_BASE_URL", value = "https://us.cloud.langfuse.com" },
  ]

  # Every SSM parameter the container should pull at start. The ECS
  # agent reads these *via the task execution role* and exposes them
  # as env vars to the container.
  task_secrets = concat(
    [
      {
        name      = "DATABASE_URL"
        valueFrom = aws_ssm_parameter.database_url.arn
      },
    ],
    [
      for k in local.secret_keys : {
        name      = k
        valueFrom = aws_ssm_parameter.secret[k].arn
      }
    ],
  )
}

resource "aws_ecs_task_definition" "agent_platform" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory_mb
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task_runtime.arn

  # ARM64 Fargate — ~20% cheaper than X86_64 and matches Apple Silicon
  # Macs (no need for `docker buildx --platform linux/amd64` cross-builds
  # during dev). The pushed image must also be ARM64; the local build on
  # an M-series Mac produces that natively.
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name      = "agent-platform"
      image     = "${aws_ecr_repository.agent_platform.repository_url}:${var.image_tag}"
      essential = true

      portMappings = [
        {
          containerPort = var.container_port
          protocol      = "tcp"
        },
      ]

      environment = local.task_env
      secrets     = local.task_secrets

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.agent_platform.name
          awslogs-region        = var.region
          awslogs-stream-prefix = "ecs"
        }
      }

      # Container-level health check supplements the ALB target group's
      # check; ECS marks the task unhealthy and replaces it if this
      # consistently fails. Same /health endpoint as the ALB uses.
      healthCheck = {
        command     = ["CMD-SHELL", "curl -fsS http://127.0.0.1:${var.container_port}/health || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }
    },
  ])
}

# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

resource "aws_ecs_service" "agent_platform" {
  name            = local.name
  cluster         = aws_ecs_cluster.agent_platform.id
  task_definition = aws_ecs_task_definition.agent_platform.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  # ECS Exec lets us `aws ecs execute-command` into the running container
  # for ad-hoc operations (one-time data migration, debugging). Pairs
  # with the ssmmessages:* permissions on the task runtime role.
  enable_execute_command = true

  # ECS deployment circuit breaker: if a new task fails health checks
  # repeatedly, ECS rolls back to the previous task definition
  # automatically rather than leaving the service stuck.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.ecs_task.id]
    assign_public_ip = true   # required for Fargate without NAT
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.agent_platform.arn
    container_name   = "agent-platform"
    container_port   = var.container_port
  }

  health_check_grace_period_seconds = 60

  # CI/CD updates the task definition's `image` field — but the service
  # tracks the latest task def revision, so we don't want Terraform to
  # roll back to whatever's in `var.image_tag` on every apply.
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [aws_lb_listener.https]
}
