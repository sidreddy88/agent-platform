#!/usr/bin/env bash
# One-shot bootstrap. Creates the S3 bucket and DynamoDB lock table that
# the Terraform `s3` backend (backend.tf) depends on. Run this once per
# AWS account, before the first `terraform init`.
#
# Why we can't manage the state backend in Terraform itself:
# chicken-and-egg — Terraform needs the bucket + table to *exist* in
# order to read/write state, but you'd need state to track them. So
# they're created out of band, then referenced from backend.tf.
#
# Usage:
#   AWS_REGION=us-east-1 ./bootstrap_state_backend.sh
#
# After this completes, edit backend.tf and replace the
# `agent-platform-tfstate-PLACEHOLDER` bucket name with the printed
# value (it includes the account ID for global uniqueness), then run
# `terraform init`.

set -euo pipefail

: "${AWS_REGION:=us-east-1}"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET_NAME="agent-platform-tfstate-${ACCOUNT_ID}"
LOCK_TABLE="agent-platform-tflock"

echo "→ AWS account : ${ACCOUNT_ID}"
echo "→ Region      : ${AWS_REGION}"
echo "→ Bucket      : ${BUCKET_NAME}"
echo "→ Lock table  : ${LOCK_TABLE}"
echo

# ------------------------------------------------------------------
# S3 bucket — versioning ON (so corrupt state can be rolled back),
# default-encrypted (AES256), public-access blocked.
# ------------------------------------------------------------------

if aws s3api head-bucket --bucket "${BUCKET_NAME}" 2>/dev/null; then
  echo "✓ S3 bucket ${BUCKET_NAME} already exists — skipping create"
else
  echo "→ Creating S3 bucket..."
  if [[ "${AWS_REGION}" == "us-east-1" ]]; then
    aws s3api create-bucket \
      --bucket "${BUCKET_NAME}" \
      --region "${AWS_REGION}"
  else
    aws s3api create-bucket \
      --bucket "${BUCKET_NAME}" \
      --region "${AWS_REGION}" \
      --create-bucket-configuration "LocationConstraint=${AWS_REGION}"
  fi
fi

aws s3api put-bucket-versioning \
  --bucket "${BUCKET_NAME}" \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption \
  --bucket "${BUCKET_NAME}" \
  --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

aws s3api put-public-access-block \
  --bucket "${BUCKET_NAME}" \
  --public-access-block-configuration \
    'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'

echo "✓ S3 bucket configured (versioning, encryption, public-access blocked)"
echo

# ------------------------------------------------------------------
# DynamoDB lock table.
# ------------------------------------------------------------------

if aws dynamodb describe-table --table-name "${LOCK_TABLE}" --region "${AWS_REGION}" >/dev/null 2>&1; then
  echo "✓ DynamoDB table ${LOCK_TABLE} already exists — skipping create"
else
  echo "→ Creating DynamoDB lock table..."
  aws dynamodb create-table \
    --table-name "${LOCK_TABLE}" \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region "${AWS_REGION}" \
    --output text >/dev/null

  echo "→ Waiting for table to become ACTIVE..."
  aws dynamodb wait table-exists \
    --table-name "${LOCK_TABLE}" \
    --region "${AWS_REGION}"
  echo "✓ Lock table ready"
fi
echo

echo "Bootstrap complete. Next:"
echo "  1. Edit backend.tf — replace agent-platform-tfstate-PLACEHOLDER with:"
echo "       bucket = \"${BUCKET_NAME}\""
echo "  2. terraform init"
echo "  3. terraform plan -var \"db_password=\$(openssl rand -hex 16)\""
