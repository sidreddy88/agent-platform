provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# Discoverable values used across the rest of the module.
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  # Lean to two AZs; enough for ALB + RDS subnet group requirements.
  azs        = slice(data.aws_availability_zones.available.names, 0, 2)
  name       = "${var.project}-${var.environment}"
}
