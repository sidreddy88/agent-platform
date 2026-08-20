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
  # AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY deliberately removed (were here
  # historically, predating task_runtime's IAM role). Real production bug
  # found rolling out the target-harness S3 fetch: AWSService (app/services/
  # aws.py) and boto3's default credential chain both check explicit
  # env-var/static credentials BEFORE falling back to the task's IAM role --
  # so as long as these were set, every AWS SDK call in this app silently
  # used this narrower, unmanaged static identity instead of task_runtime,
  # no matter what permissions were granted to the role. A new S3 read
  # policy added to task_runtime for the harness fetch never had a chance to
  # apply; the fetch failed with AccessDenied, correctly caught by the fetch
  # script's fail-loud design. Confirmed via a full audit of app/services/
  # aws.py before removing these: it only ever calls ecs/ec2/cloudwatch/logs
  # APIs, and task_runtime's policy (iam.tf) already grants every one of
  # them. Removing these two keys means every AWS SDK call in this app now
  # goes through task_runtime's role -- the intended design all along, and
  # the same IAM-native philosophy this whole harness-split effort is built
  # on (see target_harness.tf, docs/PLAN_TARGET_HARNESS_SPLIT.md).
  secret_keys = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GITHUB_TOKEN",
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
