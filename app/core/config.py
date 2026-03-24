from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    anthropic_api_key: str = ""
    github_token: str = ""
    openai_api_key: str = ""
    codebase_path: str = "/Users/Sidreddy/VoyageCode/SerpApiTestTool"
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

    # GitHub webhook secret (optional — leave blank to skip signature verification)
    github_webhook_secret: str = ""

    # Detection poll interval in seconds
    detection_poll_interval_seconds: int = 60

    class Config:
        env_file = ".env"


settings = Settings()
