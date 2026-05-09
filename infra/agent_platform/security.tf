# Security groups — chained, least-privilege.
#
# Internet → ALB:    80 / 443 from 0.0.0.0/0
# ALB → ECS task:    container_port from alb_sg only
# ECS task → RDS:    5432 from ecs_task_sg only
#
# The egress rules are wide-open (0.0.0.0/0) so the ECS task can reach
# Anthropic / OpenAI / GitHub / Atlas without per-destination ACLs.

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb-sg"
  description = "Allow HTTP/HTTPS from the internet to the ALB"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTP for the redirect listener"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "ALB → ECS tasks"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-alb-sg" }
}

resource "aws_security_group" "ecs_task" {
  name        = "${local.name}-task-sg"
  description = "Allow ALB ingress on the container port; egress to anywhere (LLM APIs, GitHub, etc.)"
  vpc_id      = aws_vpc.main.id

  egress {
    description = "Outbound to LLM/GitHub/Atlas/etc."
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-task-sg" }
}

resource "aws_security_group_rule" "task_ingress_from_alb" {
  description              = "App container port from the ALB only"
  type                     = "ingress"
  from_port                = var.container_port
  to_port                  = var.container_port
  protocol                 = "tcp"
  security_group_id        = aws_security_group.ecs_task.id
  source_security_group_id = aws_security_group.alb.id
}

resource "aws_security_group" "rds" {
  name        = "${local.name}-rds-sg"
  description = "Postgres ingress from ECS tasks only"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-rds-sg" }
}

resource "aws_security_group_rule" "rds_ingress_from_tasks" {
  description              = "Postgres only from ECS tasks"
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.rds.id
  source_security_group_id = aws_security_group.ecs_task.id
}
