# `infra/agent_platform/` — production AWS deployment

End state: agent platform on ECS Fargate at **https://app.remediatelabs.io**, behind Cloudflare Access. Lean sizing (~$70/mo).

```
client → Cloudflare (proxy + Access)
       → ALB (HTTPS via ACM)
       → ECS Fargate task (FastAPI + bundled React frontend)
       → RDS Postgres + pgvector  (state)
       → SSM Parameter Store      (secrets)
       → CloudWatch Logs          (container output)
```

---

## One-time prerequisites

1. **Domain registered**: `remediatelabs.io` (✓ done).
2. **Domain DNS in Cloudflare**: in the Cloudflare dashboard, add the zone `remediatelabs.io`. Cloudflare gives you 2 nameservers — set those at your registrar. Wait for the zone status to flip to **Active** (5–60 min).
3. **AWS CLI configured** with credentials that can create VPC / ALB / RDS / ECS / IAM / SSM / ECR / CloudWatch / DynamoDB / S3.
4. **Terraform 1.9+** installed locally.

---

## Bootstrap (one-shot, per AWS account)

The Terraform `s3` backend needs the state bucket + lock table to *exist before* `terraform init`. The bootstrap script creates them out of band:

```bash
cd infra/agent_platform
AWS_REGION=us-east-1 ./bootstrap_state_backend.sh
```

The script prints the bucket name (it includes your AWS account ID for global uniqueness). Edit `backend.tf` and replace `agent-platform-tfstate-PLACEHOLDER` with that name, then commit.

---

## First apply

```bash
# 1. Generate a strong RDS password and stash it somewhere safe — you
#    won't see it again, and rotating it requires both an SSM update
#    and a service restart.
export TF_VAR_db_password=$(openssl rand -hex 16)
echo "RDS password: ${TF_VAR_db_password}"   # save to your password manager

# 2. Initialise + plan
terraform init
terraform plan

# 3. Apply (5–15 min — most of the time is RDS create + ACM validation
#    waiting for the DNS record to propagate).
terraform apply
```

---

## During apply: ACM validation CNAMEs

Apply will pause at `aws_acm_certificate_validation.app` while it waits for the DNS validation record to resolve. In a separate terminal:

```bash
terraform output acm_validation_records
# e.g. [
#   {
#     "name":  "_abc123.app.remediatelabs.io.",
#     "type":  "CNAME",
#     "value": "_xyz789.acm-validations.aws."
#   }
# ]
```

In the Cloudflare dashboard for `remediatelabs.io`:

1. Go to **DNS → Records → Add record**
2. Type `CNAME`, name `_abc123.app` (the part before `.remediatelabs.io.`), target `_xyz789.acm-validations.aws.`
3. **Proxy status: DNS only** — the orange cloud must be OFF for ACM to read it
4. Save

ACM polls every 30s; the listener will come up within a few minutes.

---

## After apply: SSM secrets

Set every secret value the task definition pulls. The Terraform created the parameter resources with placeholders; you fill the real values via the AWS CLI (so they never live in `.tfvars` or git):

```bash
aws ssm put-parameter --overwrite --type SecureString \
  --name /agent-platform/ANTHROPIC_API_KEY    --value "sk-ant-..."

aws ssm put-parameter --overwrite --type SecureString \
  --name /agent-platform/OPENAI_API_KEY       --value "sk-..."

aws ssm put-parameter --overwrite --type SecureString \
  --name /agent-platform/GITHUB_TOKEN         --value "ghp_..."

aws ssm put-parameter --overwrite --type SecureString \
  --name /agent-platform/AWS_ACCESS_KEY_ID    --value "AKIA..."

aws ssm put-parameter --overwrite --type SecureString \
  --name /agent-platform/AWS_SECRET_ACCESS_KEY --value "..."

aws ssm put-parameter --overwrite --type SecureString \
  --name /agent-platform/ATLAS_PUBLIC_KEY     --value "..."
aws ssm put-parameter --overwrite --type SecureString \
  --name /agent-platform/ATLAS_PRIVATE_KEY    --value "..."
aws ssm put-parameter --overwrite --type SecureString \
  --name /agent-platform/ATLAS_PROJECT_ID     --value "..."

aws ssm put-parameter --overwrite --type SecureString \
  --name /agent-platform/DO_API_TOKEN         --value "..."

aws ssm put-parameter --overwrite --type SecureString \
  --name /agent-platform/CLOUDFLARE_API_TOKEN --value "..."

aws ssm put-parameter --overwrite --type SecureString \
  --name /agent-platform/LANGFUSE_PUBLIC_KEY  --value "pk-lf-..."
aws ssm put-parameter --overwrite --type SecureString \
  --name /agent-platform/LANGFUSE_SECRET_KEY  --value "sk-lf-..."

aws ssm put-parameter --overwrite --type SecureString \
  --name /agent-platform/GITHUB_WEBHOOK_SECRET --value "$(openssl rand -hex 32)"
aws ssm put-parameter --overwrite --type SecureString \
  --name /agent-platform/SLACK_WEBHOOK_URL    --value "https://hooks.slack.com/..."
aws ssm put-parameter --overwrite --type SecureString \
  --name /agent-platform/CLOUDWATCH_WEBHOOK_TOKEN --value "$(openssl rand -hex 32)"
```

The `DATABASE_URL` SSM parameter is populated by Terraform itself — don't override.

---

## Push the first image

```bash
# Login + push
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin "$(terraform output -raw ecr_repository_url)"

docker build -t "$(terraform output -raw ecr_repository_url):initial" -f ../../Dockerfile ../..
docker push "$(terraform output -raw ecr_repository_url):initial"

# Trigger the service to pull it
aws ecs update-service \
  --cluster "$(terraform output -raw ecs_cluster_name)" \
  --service "$(terraform output -raw ecs_service_name)" \
  --force-new-deployment
```

This becomes a GitHub Actions workflow in **PR 6**.

---

## Wire Cloudflare DNS + Access

In the Cloudflare dashboard for `remediatelabs.io`:

### 1. App CNAME (proxied)

DNS → Records → Add record:
- **Type**: CNAME
- **Name**: `app`
- **Target**: `<terraform output alb_dns_name>` (e.g. `agent-platform-alb-1234567890.us-east-1.elb.amazonaws.com`)
- **Proxy status**: **Proxied** (orange cloud ON)

### 2. SSL/TLS mode

SSL/TLS → Overview → set encryption mode to **Full (strict)**. Without strict, Cloudflare won't validate the ACM cert at the origin.

### 3. Cloudflare Access application

Zero Trust → Access → Applications → **Add application** → **Self-hosted**:
- **Application name**: Agent Platform
- **Application domain**: `app.remediatelabs.io`
- **Identity provider**: Google (or whatever you prefer)

Add a **policy**:
- **Policy name**: Owner + invited interviewers
- **Action**: Allow
- **Include**: emails (yourself, plus any interviewer emails you want to share with)

Add **bypass policies** for the paths that must work without auth:
- `/health`
- `/webhooks/cloudwatch-alarm`
- `/webhooks/github`

(Add bypass policies via the Application → Policies tab → Add policy → Action: Bypass → Include: Everyone.)

### 4. Run the migration script

Once the service is healthy and reachable at `https://app.remediatelabs.io/health`:

```bash
# From the repo root
DB_URL="postgresql+psycopg://agent_platform:${TF_VAR_db_password}@$(terraform -chdir=infra/agent_platform output -raw rds_address):5432/agent_platform"

python scripts/migrate_to_postgres.py --target "${DB_URL}" --dry-run
python scripts/migrate_to_postgres.py --target "${DB_URL}"
```

---

## Day-2 operations

### View container logs
```bash
aws logs tail "$(terraform output -raw log_group_name)" --follow
```

### Roll out a new image (until PR 6 lands)
```bash
docker build -t "$(terraform output -raw ecr_repository_url):$(git rev-parse --short HEAD)" -f ../../Dockerfile ../..
docker push    "$(terraform output -raw ecr_repository_url):$(git rev-parse --short HEAD)"
aws ecs update-service \
  --cluster "$(terraform output -raw ecs_cluster_name)" \
  --service "$(terraform output -raw ecs_service_name)" \
  --force-new-deployment
```

### Rotate the RDS password
```bash
NEW=$(openssl rand -hex 16)
aws rds modify-db-instance --db-instance-identifier agent-platform-prod \
  --master-user-password "${NEW}" --apply-immediately

aws ssm put-parameter --overwrite --type SecureString \
  --name /agent-platform/DATABASE_URL \
  --value "postgresql+psycopg://agent_platform:${NEW}@<rds_address>:5432/agent_platform"

aws ecs update-service --cluster agent-platform-prod --service agent-platform-prod \
  --force-new-deployment
```

### Destroy

The RDS instance has `deletion_protection=true`. To actually destroy:

```bash
# 1. Disable deletion protection (edit rds.tf or via console), apply.
# 2. terraform destroy
```

Final RDS snapshot is taken automatically before deletion.

---

## What's deliberately not here

- **Cloudflare resources in Terraform** — first pass uses the dashboard; a follow-up PR can wire the `cloudflare/cloudflare` provider so DNS + Access live in the same module.
- **Migrating PR #85's `infra/cloudwatch_sns_subscription.tf`** into this module — leaving standalone for now.
- **Splitting frontend to S3 + CloudFront** — bundled into the container is the simpler path (see PR 5 plan for the full reasoning).
- **Multi-AZ RDS / read replicas** — flip `multi_az = true` in `rds.tf` later if needed.
- **CI/CD pipeline** — that's PR 6.
