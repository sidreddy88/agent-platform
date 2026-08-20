# S3-backed fetch for targets/target-app/'s content, split into its own private
# repo (sidreddy88/agent-platform-target-harness) per docs/PLAN_TARGET_HARNESS_SPLIT.md.
#
# Two separate IAM principals, least-privilege in opposite directions:
#   - github_harness_sync (assumed by that repo's own CI via the OIDC provider
#     already registered in github_oidc.tf) — WRITE only, PutObject on this
#     bucket, nothing else.
#   - task_runtime (the running app) — READ only, GetObject/ListBucket on this
#     bucket, added to the same role every other AWS SDK call in this app
#     already uses. No new role, no static credential anywhere in this design.
#
# aws_iam_openid_connect_provider.github is defined once in github_oidc.tf and
# reused here — GitHub's OIDC provider is a single account-level resource, not
# one per trusting repo.

variable "target_harness_repo" {
  description = "owner/repo allowed to push harness content to S3 via OIDC."
  type        = string
  default     = "sidreddy88/agent-platform-target-harness"
}

resource "aws_s3_bucket" "target_harness" {
  bucket = "${local.name}-target-harness"
}

resource "aws_s3_bucket_versioning" "target_harness" {
  bucket = aws_s3_bucket.target_harness.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "target_harness" {
  bucket                  = aws_s3_bucket.target_harness.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------------------------------------------------------
# Write side — the harness repo's own CI syncs its content here on push.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "github_harness_sync_assume" {
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
        "repo:${var.target_harness_repo}:ref:refs/heads/main",
      ]
    }
  }
}

resource "aws_iam_role" "github_harness_sync" {
  name               = "${local.name}-github-harness-sync"
  assume_role_policy = data.aws_iam_policy_document.github_harness_sync_assume.json
  description        = "Assumed by agent-platform-target-harness's CI via OIDC to upload the harness bundle to S3."
}

data "aws_iam_policy_document" "github_harness_sync" {
  statement {
    sid       = "HarnessBundleUpload"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.target_harness.arn}/*"]
  }
  statement {
    sid       = "HarnessBundleList"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.target_harness.arn]
  }
}

resource "aws_iam_policy" "github_harness_sync" {
  name        = "${local.name}-github-harness-sync"
  description = "Write-only access to the target-harness S3 bucket, for the harness repo's CI."
  policy      = data.aws_iam_policy_document.github_harness_sync.json
}

resource "aws_iam_role_policy_attachment" "github_harness_sync" {
  role       = aws_iam_role.github_harness_sync.name
  policy_arn = aws_iam_policy.github_harness_sync.arn
}

# ---------------------------------------------------------------------------
# Read side — the running app fetches the bundle at container startup.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "task_runtime_harness_read" {
  statement {
    sid       = "HarnessBundleDownload"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.target_harness.arn}/*"]
  }
  statement {
    sid       = "HarnessBundleReadList"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.target_harness.arn]
  }
}

resource "aws_iam_policy" "task_runtime_harness_read" {
  name        = "${local.name}-task-runtime-harness-read"
  description = "Read-only access to the target-harness S3 bucket, for the running app's startup fetch."
  policy      = data.aws_iam_policy_document.task_runtime_harness_read.json
}

resource "aws_iam_role_policy_attachment" "task_runtime_harness_read" {
  role       = aws_iam_role.task_runtime.name
  policy_arn = aws_iam_policy.task_runtime_harness_read.arn
}

output "target_harness_bucket" {
  description = "S3 bucket the target-harness repo's CI pushes to, and the app fetches from at startup."
  value       = aws_s3_bucket.target_harness.bucket
}

output "github_harness_sync_role_arn" {
  description = "Role ARN to set as ROLE_ARN in agent-platform-target-harness's sync workflow."
  value       = aws_iam_role.github_harness_sync.arn
}
