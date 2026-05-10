#!/usr/bin/env bash
# One-shot migration: local SQLite + ChromaDB → RDS Postgres + pgvector.
#
# Briefly opens the RDS instance to your home IP so the migration script
# can reach it from your laptop, then reverts. The window is ~10 minutes
# (RDS modify-db-instance takes 3–5 min each direction). Your IP is the
# only one allowed during that window; everyone else is still blocked.
#
# Usage:
#   AWS_PROFILE=agent-platform ./scripts/migrate_local_to_rds.sh
#
# What it does, step by step:
#   1. Looks up your public IP via ifconfig.me
#   2. Looks up the RDS instance + RDS security group
#   3. Adds an ingress rule: 5432 from <your-ip>/32 to RDS SG
#   4. Modifies RDS to publicly_accessible=true (apply-immediately)
#   5. Waits for RDS to be available (~3–5 min)
#   6. Pulls DATABASE_URL from SSM
#   7. Rewrites the host portion of DATABASE_URL to use the *public* DNS
#      (RDS endpoint stays the same name but resolves to a public IP now)
#   8. Runs `python scripts/migrate_to_postgres.py --target $URL --dry-run`
#   9. Pauses — you confirm row counts look right
#  10. Runs the real migration
#  11. Reverts: publicly_accessible=false, removes the SG rule
#  12. Waits until RDS is back to private
#  13. Confirms reversion via aws ec2 describe-security-groups
#
# Anything that fails mid-run leaves diagnostic state in /tmp/migrate-*

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="/tmp/migrate-rds-$(date +%s).log"

: "${AWS_PROFILE:=agent-platform}"
: "${AWS_REGION:=us-east-1}"
DB_ID="agent-platform-prod"
RDS_SG_NAME="agent-platform-prod-rds-sg"

log() { echo "$(date +%H:%M:%S) $*" | tee -a "$LOG"; }
fail() { log "✗ $*"; exit 1; }

trap 'log "Run log: $LOG"' EXIT

log "→ Migration log: $LOG"
log "→ AWS profile  : $AWS_PROFILE"
log "→ Region       : $AWS_REGION"
log "→ RDS instance : $DB_ID"
log

# -----------------------------------------------------------------------
# Sanity checks
# -----------------------------------------------------------------------
command -v aws >/dev/null  || fail "aws CLI not on PATH"
command -v jq  >/dev/null  || fail "jq not on PATH (brew install jq)"
[ -f "$REPO_ROOT/agent_platform.db" ] || fail "missing $REPO_ROOT/agent_platform.db"
[ -d "$REPO_ROOT/.chromadb" ]         || log  "(no .chromadb/ — vector migration will be skipped)"

# Confirm the RDS instance exists in the configured profile.
aws rds describe-db-instances --db-instance-identifier "$DB_ID" --no-cli-pager >/dev/null 2>&1 \
  || fail "RDS instance $DB_ID not visible from profile $AWS_PROFILE"

# -----------------------------------------------------------------------
# Step 1: my IPv4 (RDS public access uses IPv4 only — IPv6 endpoints
# would need separate Ipv6Ranges authorize calls and aren't supported by
# default RDS). Try multiple IPv4-only sources in case one is down.
# -----------------------------------------------------------------------
MY_IP=""
for url in https://ipv4.icanhazip.com https://api.ipify.org https://checkip.amazonaws.com; do
  candidate=$(curl -sS --max-time 10 "$url" 2>/dev/null | tr -d '[:space:]') || true
  # Strict IPv4 regex — rejects IPv6 like 2601:2c3:...
  if [[ "$candidate" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    MY_IP="$candidate"
    break
  fi
done
[ -n "$MY_IP" ] || fail "could not resolve an IPv4 address from any of ipv4.icanhazip.com / api.ipify.org / checkip.amazonaws.com"
log "→ My public IPv4 : $MY_IP"

# -----------------------------------------------------------------------
# Step 2: RDS SG
# -----------------------------------------------------------------------
RDS_SG=$(aws ec2 describe-security-groups \
  --filters "Name=tag:Name,Values=$RDS_SG_NAME" \
  --query 'SecurityGroups[0].GroupId' --output text --no-cli-pager) || fail "could not look up RDS SG"
[ "$RDS_SG" = "None" ] && fail "RDS SG $RDS_SG_NAME not found"
log "→ RDS SG       : $RDS_SG"

# -----------------------------------------------------------------------
# Confirm with the operator before any production change.
# -----------------------------------------------------------------------
echo
echo "About to:"
echo "  • Allow $MY_IP/32 → port 5432 on $RDS_SG"
echo "  • Modify RDS instance $DB_ID to publicly_accessible=true (5–10 min)"
echo "  • Run migration"
echo "  • Revert all of the above"
echo
read -r -p "Proceed? [y/N] " ans
[[ "$ans" =~ ^[Yy]$ ]] || { log "aborted"; exit 0; }

# -----------------------------------------------------------------------
# Step 3 + 4: open RDS to my IP
# -----------------------------------------------------------------------
log
log "→ Adding SG ingress rule ($MY_IP/32 → 5432)..."
authorize_out=$(aws ec2 authorize-security-group-ingress \
  --group-id "$RDS_SG" --protocol tcp --port 5432 --cidr "$MY_IP/32" \
  --no-cli-pager 2>&1) || true
echo "$authorize_out" >>"$LOG"

if echo "$authorize_out" | grep -q "InvalidPermission.Duplicate"; then
  log "  (SG rule already exists from a prior attempt — continuing)"
elif echo "$authorize_out" | grep -q '"Return": true\|securityGroupRuleId'; then
  log "  ✓ ingress rule added"
else
  log "✗ authorize-security-group-ingress failed:"
  echo "$authorize_out" | tee -a "$LOG"
  fail "could not open RDS to your IP — see error above"
fi

log "→ Setting RDS publicly_accessible=true (apply-immediately)..."
aws rds modify-db-instance \
  --db-instance-identifier "$DB_ID" \
  --publicly-accessible \
  --apply-immediately \
  --no-cli-pager --output json >>"$LOG" 2>&1 || fail "modify-db-instance failed"

log "→ Waiting for RDS to apply (3–5 min)..."
aws rds wait db-instance-available --db-instance-identifier "$DB_ID" \
  || fail "RDS did not become available"

# -----------------------------------------------------------------------
# Step 6 + 7: build DATABASE_URL using the now-public endpoint.
# -----------------------------------------------------------------------
log "→ Pulling DATABASE_URL from SSM..."
DB_URL=$(aws ssm get-parameter \
  --name /agent-platform/DATABASE_URL --with-decryption \
  --query 'Parameter.Value' --output text --no-cli-pager) \
  || fail "could not read DATABASE_URL from SSM"

# RDS endpoint host is unchanged when toggling publicly-accessible — the
# DNS name now resolves to a public IP from outside the VPC. Confirm the
# host resolves before we attempt to connect.
RDS_HOST=$(echo "$DB_URL" | sed -E 's#.*@([^:/]+).*#\1#')
log "→ Verifying $RDS_HOST resolves..."
host "$RDS_HOST" >>"$LOG" 2>&1 || dig +short "$RDS_HOST" >>"$LOG" 2>&1 || true

# -----------------------------------------------------------------------
# Step 8 + 9: dry run
# -----------------------------------------------------------------------
echo
log "→ Dry run — counting rows / vectors..."
cd "$REPO_ROOT"
python scripts/migrate_to_postgres.py --target "$DB_URL" --dry-run \
  || { log "✗ dry run failed"; }

echo
read -r -p "Counts look right? Run the real migration? [y/N] " ans
if [[ "$ans" =~ ^[Yy]$ ]]; then
  log "→ Running real migration..."
  python scripts/migrate_to_postgres.py --target "$DB_URL" \
    || log "✗ migration script returned non-zero — review log"
else
  log "skipping real migration; will still revert RDS to private"
fi

# -----------------------------------------------------------------------
# Step 11 + 12: revert
# -----------------------------------------------------------------------
log
log "→ Reverting RDS to publicly_accessible=false..."
aws rds modify-db-instance \
  --db-instance-identifier "$DB_ID" \
  --no-publicly-accessible \
  --apply-immediately \
  --no-cli-pager --output json >>"$LOG" 2>&1 || log "✗ revert modify failed (run manually)"

log "→ Waiting for RDS to apply..."
aws rds wait db-instance-available --db-instance-identifier "$DB_ID" \
  || log "✗ RDS did not finish reverting — verify manually"

log "→ Removing SG ingress rule for $MY_IP/32..."
aws ec2 revoke-security-group-ingress \
  --group-id "$RDS_SG" --protocol tcp --port 5432 --cidr "$MY_IP/32" \
  --no-cli-pager >>"$LOG" 2>&1 || log "✗ SG rule revoke failed (run manually)"

# -----------------------------------------------------------------------
# Step 13: verify reverted state
# -----------------------------------------------------------------------
log
log "→ Verification:"
PUB=$(aws rds describe-db-instances --db-instance-identifier "$DB_ID" \
  --query 'DBInstances[0].PubliclyAccessible' --output text --no-cli-pager)
log "  RDS publicly_accessible : $PUB  (expected: False)"

INGRESS=$(aws ec2 describe-security-groups --group-ids "$RDS_SG" \
  --query "SecurityGroups[0].IpPermissions[?FromPort==\`5432\`].IpRanges[].CidrIp" --output text --no-cli-pager)
log "  RDS SG IPv4 ingress     : ${INGRESS:-(none)}  (expected: empty — only ECS task SG inbound remains)"

if [ "$PUB" = "False" ] && [ -z "$INGRESS" ]; then
  log "✓ Revert complete — RDS is private again."
else
  log "✗ Revert incomplete — investigate."
  exit 2
fi

log
log "Done. Full log: $LOG"
