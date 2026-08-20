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
  #
  # AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY: removed once (predating
  # task_runtime's IAM role, believed to be pure legacy) then restored here.
  # The removal fixed a real bug -- boto3's default credential chain and
  # AWSService both check explicit env-var/static credentials BEFORE the
  # task's IAM role, so their presence silently shadowed a new S3 read
  # policy added to task_runtime for the harness fetch (AccessDenied on
  # every fetch attempt). But the audit behind that removal only checked
  # that task_runtime's IN-ACCOUNT policy covered every AWS API action
  # AWSService calls -- it didn't check which AWS ACCOUNT those calls need
  # to reach. The target app's real CloudWatch log group/alarms/SNS topic
  # live in a separate AWS account (950252867672) from this one
  # (542337758768, see infra/target_monitoring). An IAM role in this
  # account can never read another account's CloudWatch Logs no matter what
  # permissions it's granted -- only a static credential scoped to that
  # other account (or a proper cross-account trust relationship, not yet
  # built) can. Removing these broke DetectionService/TriageAgent/
  # DiagnosisAgent's ability to read that log group in production.
  #
  # These are restored for that cross-account reason only.
  # scripts/fetch_target_harness.py explicitly strips both from its own
  # environment before constructing its S3 client, so their presence here
  # can't shadow task_runtime's role for the (same-account) harness fetch
  # again -- see the comment at that call site.
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
