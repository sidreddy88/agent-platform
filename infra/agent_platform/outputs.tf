output "alb_dns_name" {
  description = "ALB DNS hostname. Add a Cloudflare CNAME `app` -> this value (proxied)."
  value       = aws_lb.main.dns_name
}

output "alb_zone_id" {
  description = "ALB hosted zone ID — useful if you ever switch to Route53 alias records."
  value       = aws_lb.main.zone_id
}

output "acm_validation_records" {
  description = "ACM cert validation CNAMEs to create in Cloudflare DNS (DNS-only, NOT proxied)."
  value = [
    for o in aws_acm_certificate.app.domain_validation_options : {
      name  = o.resource_record_name
      type  = o.resource_record_type
      value = o.resource_record_value
    }
  ]
}

output "ecr_repository_url" {
  description = "ECR repo URL — push images here, then update the ECS service."
  value       = aws_ecr_repository.agent_platform.repository_url
}

output "ecs_cluster_name" {
  description = "ECS cluster name — pass to `aws ecs update-service --cluster ...`."
  value       = aws_ecs_cluster.agent_platform.name
}

output "ecs_service_name" {
  description = "ECS service name — pass to `aws ecs update-service --service ...`."
  value       = aws_ecs_service.agent_platform.name
}

output "rds_endpoint" {
  description = "RDS endpoint (host:port). Used by the migrate_to_postgres.py script."
  value       = aws_db_instance.main.endpoint
}

output "rds_address" {
  description = "RDS hostname only, no port."
  value       = aws_db_instance.main.address
}

output "database_url_template" {
  description = "DATABASE_URL shape — the real URL with the password is in SSM at /agent-platform/DATABASE_URL."
  value       = "postgresql+psycopg://${var.db_username}:<password>@${aws_db_instance.main.address}:${aws_db_instance.main.port}/${var.db_name}"
  sensitive   = false
}

output "task_execution_role_arn" {
  description = "Used by GitHub Actions for ECS deploys — give the OIDC role iam:PassRole on this."
  value       = aws_iam_role.task_execution.arn
}

output "task_runtime_role_arn" {
  description = "Application's runtime IAM role — what the agents see when they call the AWS SDK."
  value       = aws_iam_role.task_runtime.arn
}

output "log_group_name" {
  description = "CloudWatch Logs group for the ECS task."
  value       = aws_cloudwatch_log_group.agent_platform.name
}

output "app_url" {
  description = "Public app URL once Cloudflare DNS is wired and the cert is validated."
  value       = "https://${var.hostname}"
}
