#!/usr/bin/env bash
# Reads each known secret from .env and pushes it to AWS SSM Parameter
# Store under /agent-platform/<KEY>. Idempotent — `--overwrite` updates
# values in place. Values are never echoed to stdout.
#
# Usage:
#   AWS_PROFILE=agent-platform ./scripts/push_ssm_secrets.sh
#
# Requires:
#   - .env file in repo root with the keys listed in SECRETS below
#   - aws CLI configured (AWS_PROFILE or default profile)
#   - The Terraform apply must have already created the placeholder
#     SSM parameters; this script just overwrites their values.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"

# Mirrors infra/agent_platform/secrets.tf `local.secret_keys`. DATABASE_URL
# is set by Terraform itself — don't push it from .env.
SECRETS=(
  ANTHROPIC_API_KEY
  OPENAI_API_KEY
  GITHUB_TOKEN
  AWS_ACCESS_KEY_ID
  AWS_SECRET_ACCESS_KEY
  ATLAS_PUBLIC_KEY
  ATLAS_PRIVATE_KEY
  ATLAS_PROJECT_ID
  DO_API_TOKEN
  CLOUDFLARE_API_TOKEN
  LANGFUSE_PUBLIC_KEY
  LANGFUSE_SECRET_KEY
  GITHUB_WEBHOOK_SECRET
  SLACK_WEBHOOK_URL
  CLOUDWATCH_WEBHOOK_TOKEN
)

if [ ! -f "${ENV_FILE}" ]; then
  echo "✗ ${ENV_FILE} not found" >&2
  exit 1
fi

if ! command -v aws >/dev/null 2>&1; then
  echo "✗ aws CLI not on PATH" >&2
  exit 1
fi

# Helper: read a single key's value from .env without sourcing it
# (sourcing breaks on unquoted commas, parens, etc.). Strips matching
# surrounding quotes if present.
read_env_value() {
  local key="$1"
  local line value
  line=$(grep -E "^[[:space:]]*${key}=" "${ENV_FILE}" | head -1 || true)
  [ -z "${line}" ] && return 1
  # Drop everything up to and including the first '='
  value="${line#*=}"
  # Strip matching surrounding quotes
  case "${value}" in
    \"*\") value="${value#\"}"; value="${value%\"}" ;;
    \'*\') value="${value#\'}"; value="${value%\'}" ;;
  esac
  printf '%s' "${value}"
}

pushed=0
skipped=0
empty=0

for key in "${SECRETS[@]}"; do
  value=$(read_env_value "${key}" || true)
  if [ -z "${value}" ]; then
    printf "  · %-28s (empty in .env — skipping)\n" "${key}"
    empty=$((empty + 1))
    continue
  fi

  # Push without echoing the value. --no-cli-pager avoids interactive less.
  if aws ssm put-parameter \
      --name "/agent-platform/${key}" \
      --type SecureString \
      --value "${value}" \
      --overwrite \
      --no-cli-pager \
      --output text >/dev/null 2>&1; then
    printf "  ✓ %-28s pushed\n" "${key}"
    pushed=$((pushed + 1))
  else
    printf "  ✗ %-28s FAILED (parameter may not exist yet — run terraform apply first)\n" "${key}"
    skipped=$((skipped + 1))
  fi
done

echo
echo "Summary: ${pushed} pushed, ${empty} empty, ${skipped} failed"

if [ "${skipped}" -gt 0 ]; then
  exit 2
fi
