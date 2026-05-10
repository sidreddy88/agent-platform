# Remote state — S3 + DynamoDB lock.
#
# Bootstrap is a one-shot run via ./bootstrap_state_backend.sh, which
# creates the bucket and lock table before this backend can resolve.
# After bootstrap, every `terraform init` reads/writes here.
#
# Why not local state: a `.tfstate` file checked into git is a
# single-developer footgun (lock contention, secrets leaked in plan
# output, drift between machines). S3 + DynamoDB is the standard.

terraform {
  backend "s3" {
    bucket         = "agent-platform-tfstate-542337758768"
    key            = "agent-platform/prod/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "agent-platform-tflock"
    encrypt        = true
  }
}
