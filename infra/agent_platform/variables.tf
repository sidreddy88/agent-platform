variable "region" {
  description = "AWS region — keep as us-east-1 to match existing CloudWatch + SNS setup."
  type        = string
  default     = "us-east-1"
}

variable "hostname" {
  description = "Public hostname the ALB serves traffic for (e.g. app.remediatelabs.io)."
  type        = string
  default     = "app.remediatelabs.io"
}

variable "project" {
  description = "Project tag applied to every resource."
  type        = string
  default     = "agent-platform"
}

variable "environment" {
  description = "Environment label used in tags and resource names."
  type        = string
  default     = "prod"
}

variable "container_port" {
  description = "Port uvicorn listens on inside the container. Matches Dockerfile EXPOSE."
  type        = number
  default     = 8000
}

variable "task_cpu" {
  description = "Fargate vCPU units (256 = 0.25 vCPU, 512 = 0.5, 1024 = 1.0). Lean default."
  type        = number
  default     = 512
}

variable "task_memory_mb" {
  description = "Fargate task memory in MB. Must be in a valid CPU/mem combo for Fargate."
  type        = number
  default     = 1024
}

variable "desired_count" {
  description = "How many ECS tasks to run. 1 is fine for the current event volume."
  type        = number
  default     = 1
}

variable "image_tag" {
  description = "Container image tag to deploy. CI overrides per-deploy; default lets the first apply succeed before any image exists."
  type        = string
  default     = "initial"
}

variable "db_password" {
  description = "RDS master password — pass via tfvars file (gitignored) or env var TF_VAR_db_password. NEVER commit."
  type        = string
  sensitive   = true
}

variable "db_username" {
  description = "RDS master username."
  type        = string
  default     = "agent_platform"
}

variable "db_name" {
  description = "Initial Postgres database name."
  type        = string
  default     = "agent_platform"
}

variable "db_instance_class" {
  description = "RDS instance class. db.t4g.micro keeps cost ~\\$13/mo."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage_gb" {
  description = "RDS storage in GB. 20GB is enough for the current 1530 rows + headroom."
  type        = number
  default     = 20
}
