variable "region" {
  description = "AWS region the target app's ECS task runs in. Same region as the log group."
  type        = string
  default     = "us-east-2"
}

variable "log_group_name" {
  description = "CloudWatch log group the target app's ECS task writes to. Real external identifier this module doesn't control the naming of -- no default on purpose, pass via tfvars file (gitignored) or TF_VAR_log_group_name."
  type        = string
}

variable "webhook_url_base" {
  description = "Base URL of the agent-platform webhook (no path, no query). The /webhooks/cloudwatch-alarm path is appended automatically."
  type        = string
  default     = "https://app.remediatelabs.io"
}

variable "webhook_token" {
  description = "Shared-secret token gating the webhook. Same value the webhook handler validates against settings.cloudwatch_webhook_token. Fetch from agent-platform SSM: aws ssm get-parameter --profile agent-platform --name /agent-platform/CLOUDWATCH_WEBHOOK_TOKEN --with-decryption --query Parameter.Value --output text"
  type        = string
  sensitive   = true
}

variable "name_prefix" {
  description = "Resource name prefix. Used for SNS topic, alarm, metric filter. No default on purpose -- pass via tfvars/TF_VAR_name_prefix."
  type        = string
}

variable "cloudwatch_namespace" {
  description = "CloudWatch custom metric namespace for the error-count metric. No default on purpose -- pass via tfvars/TF_VAR_cloudwatch_namespace."
  type        = string
}

variable "error_threshold" {
  description = "Sum-over-period threshold above which the alarm fires. Default 0 means a single error triggers an event (relies on platform dedup to coalesce). Raise to e.g. 4 to require sustained errors before firing."
  type        = number
  default     = 0
}

variable "error_period_seconds" {
  description = "Window the metric Sum is evaluated over. Default 60s matches the agent-platform self-monitor."
  type        = number
  default     = 60
}
