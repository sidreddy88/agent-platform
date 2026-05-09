# Application Load Balancer fronting the ECS service.
#
# Two listeners:
#   - 80  → 301 redirect to 443
#   - 443 → forward to the agent-platform target group (HTTPS terminated
#     here via the ACM cert; container talks plain HTTP inside the VPC)
#
# Cloudflare proxies traffic in front of this ALB. With Cloudflare in
# "Full (strict)" SSL mode, the chain is:
#   client → Cloudflare (CF cert) → ALB (ACM cert) → container (HTTP)

resource "aws_lb" "main" {
  name                       = "${local.name}-alb"
  load_balancer_type         = "application"
  internal                   = false
  security_groups            = [aws_security_group.alb.id]
  subnets                    = aws_subnet.public[*].id

  drop_invalid_header_fields = true
  enable_deletion_protection = false   # flip to true once stable
  idle_timeout               = 60
}

resource "aws_lb_target_group" "agent_platform" {
  name        = "${local.name}-tg"
  port        = var.container_port
  protocol    = "HTTP"
  target_type = "ip"     # required for Fargate
  vpc_id      = aws_vpc.main.id

  health_check {
    path                = "/health"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  deregistration_delay = 30
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate.app.arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.agent_platform.arn
  }

  # Don't try to attach the listener until the ACM cert is fully
  # validated — otherwise apply hangs.
  depends_on = [aws_acm_certificate_validation.app]
}
