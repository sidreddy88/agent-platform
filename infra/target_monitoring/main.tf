# Target app push-based error monitoring.
#
# Lives in a SEPARATE Terraform state from the main agent-platform module
# because it targets a different AWS account:
#   - agent-platform infra: account 542337758768, us-east-1
#   - target app infra:     account 950252867672, us-east-2
#
# Cross-account state would conflate blast radius and require IAM trust
# we don't currently have. Keep the boundary clean: this module is owned
# by whoever has admin credentials in the target app's account.
#
# Outcome: CloudWatch metric filter watches /ecs/TaskAllInterviews for
# Node.js error signatures, fires an alarm, alarm publishes to an SNS
# topic, SNS POSTs to the agent-platform webhook. The webhook handler
# (validated end-to-end against the agent-platform-prod SNS topic
# already) normalises the payload into a pending event in the dashboard.
#
# Zero new load on the target app's ECS task — the awslogs driver is
# already shipping its stdout/stderr to CloudWatch. The metric filter +
# alarm evaluation run on AWS-managed compute, not on the container.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "agent-platform"
      Purpose   = "allinterviews-error-monitoring"
      ManagedBy = "terraform"
    }
  }
}
