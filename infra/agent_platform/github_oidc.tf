# GitHub OIDC federation — lets the deploy workflow assume an IAM role
# directly using a short-lived OIDC token from GitHub. No long-lived
# AWS access keys stored as repo secrets.
#
# Trust is keyed to:
#   - repo:sidreddy88/agent-platform on the main branch (push deploys)
#   - repo:sidreddy88/agent-platform on workflow_dispatch from main
#
# After the first apply, copy the role ARN into
# .github/workflows/deploy.yml (already wired by name in this PR).

variable "github_repo" {
  description = "owner/repo allowed to assume the deploy role via OIDC."
  type        = string
  default     = "sidreddy88/agent-platform"
}

variable "github_branch" {
  description = "Branch ref allowed to deploy. main only."
  type        = string
  default     = "main"
}

# GitHub's public OIDC provider. Thumbprint comes from GitHub's published
# documentation; AWS verifies certs against this list when validating
# tokens.
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
  ]
}

data "aws_iam_policy_document" "github_deploy_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.github_repo}:ref:refs/heads/${var.github_branch}",
      ]
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  name               = "${local.name}-github-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_deploy_assume.json

  description = "Assumed by GitHub Actions via OIDC to push images and roll out ECS deploys."
}

# What the deploy role is allowed to do:
#   - Push/pull to the agent-platform ECR repo (and read auth tokens)
#   - UpdateService + DescribeServices on the agent-platform ECS service
#   - PassRole on the two task roles (UpdateService validates them on every
#     deploy even though we're not changing the task def shape here)
data "aws_iam_policy_document" "github_deploy" {
  # ECR auth — needs to be on `*` per AWS, the actual push/pull is
  # scoped below.
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "EcrPushPull"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:DescribeRepositories",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:ListImages",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [aws_ecr_repository.agent_platform.arn]
  }

  statement {
    sid = "EcsDeploy"
    actions = [
      "ecs:UpdateService",
      "ecs:DescribeServices",
      "ecs:DescribeTaskDefinition",
    ]
    resources = [
      aws_ecs_service.agent_platform.id,
      "arn:aws:ecs:${var.region}:${local.account_id}:task-definition/${local.name}:*",
    ]
  }

  # services-stable + health verification poll DescribeServices, which is
  # already covered above. UpdateService validates the task def's IAM
  # roles, so we need PassRole on both even when not editing the task def.
  statement {
    sid     = "PassTaskRoles"
    actions = ["iam:PassRole"]
    resources = [
      aws_iam_role.task_execution.arn,
      aws_iam_role.task_runtime.arn,
    ]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_policy" "github_deploy" {
  name        = "${local.name}-github-deploy"
  description = "Permissions for the GitHub Actions deploy role."
  policy      = data.aws_iam_policy_document.github_deploy.json
}

resource "aws_iam_role_policy_attachment" "github_deploy" {
  role       = aws_iam_role.github_deploy.name
  policy_arn = aws_iam_policy.github_deploy.arn
}

output "github_deploy_role_arn" {
  description = "Role ARN to set as ROLE_ARN in .github/workflows/deploy.yml."
  value       = aws_iam_role.github_deploy.arn
}
