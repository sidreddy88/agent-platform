from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    anthropic_api_key: str = ""
    github_token: str = ""
    openai_api_key: str = ""
    codebase_path: str = "/Users/Sidreddy/DevCode/TargetApp"
    # AWS — leave blank to use local credentials (~/.aws/credentials / env vars)
    aws_region: str = "us-east-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    # Langfuse tracing — leave blank to disable tracing
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_base_url: str = ""  # alternative to langfuse_host (used by some Langfuse setups)
    # Alerting — leave blank to disable Slack notifications (falls back to console)
    slack_webhook_url: str = ""
    # Alert thresholds
    alert_error_rate_pct: float = 10.0       # agent error rate % → error alert
    alert_latency_p95_sec: float = 30.0      # p95 latency seconds → warning alert
    alert_approval_pending_min: int = 60     # approval pending minutes → warning alert
    alert_budget_warning_pct: float = 80.0   # daily cost % of budget → warning alert
    alert_budget_critical_pct: float = 100.0 # daily cost % of budget → critical alert
    alert_daily_budget_usd: float = 10.0     # daily LLM cost budget in USD
    app_name: str = "Agent Platform"
    debug: bool = False
    environment: str = "production"  # production | development | test

    # Digital Ocean
    do_api_token: str = ""
    # WordPress sites — format: "https://site1.com|droplet_id|droplet_name, ..."
    wordpress_sites: str = ""

    # Cloudflare
    cloudflare_api_token: str = ""
    # Comma-separated zone IDs
    cloudflare_zone_ids: str = ""

    # ECS monitoring
    ecs_cluster: str = ""
    # Comma-separated ECS service names to monitor
    ecs_services: str = ""

    # Task-based ECS clusters (no services — tasks launched on demand)
    ecs_task_clusters: str = ""

    # EC2 monitoring
    # Comma-separated EC2 instance IDs to monitor
    ec2_instance_ids: str = ""
    # Override region for EC2 if different from ECS region
    ec2_region: str = ""

    # ALB monitoring — comma-separated ALB names
    alb_names: str = ""

    # CloudWatch log groups to monitor for errors — comma-separated
    ecs_log_groups: str = ""

    # Region for ecs_log_groups when it differs from aws_region — e.g. when
    # the agent-platform runs in us-east-1 but the monitored log group lives
    # in us-east-2. Empty string falls back to aws_region.
    ecs_log_groups_region: str = ""

    # Threshold monitoring
    threshold_check_interval_seconds: int = 86400  # how often to run checks (default 24 hours)
    threshold_cooldown_minutes: int = 30           # min gap between repeat alerts for same issue
    alert_do_memory_pct: float = 85.0             # DO droplet memory % threshold
    alert_alb_5xx_count: int = 10                 # ALB 5xx count in last 5 min

    # GitHub Actions monitoring
    github_actions_repo: str = ""   # format: owner/repo

    # MongoDB Atlas
    atlas_public_key: str = ""
    atlas_private_key: str = ""
    atlas_project_id: str = ""

    # GitHub webhook secret (optional — leave blank to skip signature verification)
    github_webhook_secret: str = ""

    # Base URL for human approval links in Slack messages
    approval_base_url: str = "http://localhost:8000"

    # Target repo for AI-generated fix PRs (owner/repo)
    fix_target_repo: str = "TargetOrg/TargetApp"

    # Root directory for persistent local repo clones (used by LocalRepoService).
    # Defaults to ~/.agent-platform/repos if unset.
    repo_clone_root: str = ""

    # Directory containing AGENTS.md + CONSTRAINTS.md to inject into every agent call.
    # Relative to the project root, or absolute. Set to "" to disable injection.
    harness_docs_path: str = "targets/target-app"

    # Monitor generation — set to true to actually provision CloudWatch alarms on PR merge
    create_monitors: bool = False

    # Detection poll interval in seconds. 5 min cadence: rate-limit-safe on
    # CloudWatch Logs (0.003 TPS vs 5 TPS limit) and matches the typical
    # frequency at which an TargetApp error class repeats.
    detection_poll_interval_seconds: int = 300

    # CloudWatch log filter patterns for application-level error detection.
    # JSON array: [{"log_group": "/ecs/...", "pattern": "NoSuchKey",
    #               "error_type": "S3_NO_SUCH_KEY", "service": "target-app"}]
    cw_log_filters: str = ""

    # SNS topic ARN that CloudWatch alarms publish to. When set:
    #   - MonitorGenerationAgent wires this ARN into AlarmActions on every
    #     alarm it provisions, so alarm transitions push to SNS.
    #   - The /webhooks/cloudwatch-alarm endpoint expects messages from
    #     this topic.
    # Leave blank to keep alarms inert (UI-only, no push ingest).
    cloudwatch_alarm_sns_topic_arn: str = ""

    # Shared-secret token gating /webhooks/cloudwatch-alarm. The SNS HTTPS
    # subscription must include this token in the URL (e.g. ?token=…) or
    # in the X-Webhook-Token header. Leave blank to skip token auth (not
    # recommended in production — proper SNS signature verification is
    # the long-term answer).
    cloudwatch_webhook_token: str = ""

    # SQLAlchemy connection URL.
    #   sqlite:///agent_platform.db    (default — local dev, no setup)
    #   postgresql+psycopg://user:pw@host:5432/agent_platform   (RDS in prod)
    # Empty / unset falls back to the local SQLite file for backwards
    # compatibility with existing call sites and stored data.
    database_url: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
