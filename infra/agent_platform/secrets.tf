# SSM Parameter Store — every secret the task definition needs.
#
# Terraform creates the parameter resources with placeholder values so
# the apply succeeds even before secrets are set. You then run
# `aws ssm put-parameter --overwrite ...` once per parameter to set the
# real value (documented in README.md). Real values are NEVER committed
# to git or .tfvars.
#
# `lifecycle.ignore_changes = [value]` means subsequent terraform applies
# don't try to revert the value back to the placeholder.

locals {
  # Names mirror the keys in app/core/config.py.
  secret_keys = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GITHUB_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "ATLAS_PUBLIC_KEY",
    "ATLAS_PRIVATE_KEY",
    "ATLAS_PROJECT_ID",
    "DO_API_TOKEN",
    "CLOUDFLARE_API_TOKEN",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "GITHUB_WEBHOOK_SECRET",
    "SLACK_WEBHOOK_URL",
    "CLOUDWATCH_WEBHOOK_TOKEN",
  ]
}

resource "aws_ssm_parameter" "secret" {
  for_each = toset(local.secret_keys)

  name  = "/agent-platform/${each.key}"
  type  = "SecureString"
  value = "PLACEHOLDER-set-via-aws-cli"

  lifecycle {
    ignore_changes = [value]
  }
}

# DATABASE_URL is constructed from the RDS instance + db_password.
# Stored as SSM SecureString so the task definition reads it the same
# way as every other secret.
resource "aws_ssm_parameter" "database_url" {
  name = "/agent-platform/DATABASE_URL"
  type = "SecureString"
  value = format(
    "postgresql+psycopg://%s:%s@%s:%d/%s",
    var.db_username,
    var.db_password,
    aws_db_instance.main.address,
    aws_db_instance.main.port,
    var.db_name,
  )

  lifecycle {
    # Recompute when the RDS endpoint or username changes; password
    # updates are NOT propagated automatically (rotate the parameter
    # manually + bounce the service).
    ignore_changes = [value]
  }

  depends_on = [aws_db_instance.main]
}
