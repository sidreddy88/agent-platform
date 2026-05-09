# Container log group. ECS streams every line of stdout/stderr from the
# task here.
#
# 30-day retention is plenty for incident scanning + interview demos
# without piling up storage cost.

resource "aws_cloudwatch_log_group" "agent_platform" {
  name              = "/ecs/${local.name}"
  retention_in_days = 30
}
