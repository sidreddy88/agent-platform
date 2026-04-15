"""
MonitorGenerationAgent — generates monitoring coverage from merged PR diffs.

Triggered on PR merge. Runs out-of-band — NOT in the critical incident path.

Flow:
  1. analyze_pr_diff     → list changed files, additions, new functions/endpoints
  2. generate_cloudwatch_alarms  → CloudWatch alarm configs per file
  3. generate_do_health_checks   → DO health check configs for endpoint files
  4. (optional) create_cloudwatch_alarm — dry-run gated; set CREATE_MONITORS=true to provision

Coverage target: 1 alarm per 75 lines of new code (Ramp standard).

Output:
  {
    "monitors": [...],
    "files_analyzed": <int>,
    "monitors_created": <int>,
    "coverage_ratio": <float>
  }
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from app.agents.base import BaseAgent
from app.core.config import settings
from app.services.github import GitHubError, GitHubService
from app.services.llm import HAIKU_MODEL, LLMService

logger = logging.getLogger(__name__)

# Regex patterns for detecting new function/endpoint definitions in patch hunks
_FN_PATTERNS = [
    re.compile(r"(?:def |async def )(\w+)"),
    re.compile(r"(?:function |async function )(\w+)"),
    re.compile(r"(?:router|app)\.(?:get|post|put|delete|patch)\(['\"]([^'\"]+)['\"]"),
    re.compile(r"export (?:default |const |function |async function )(\w+)"),
]


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class MonitorConfig:
    monitor_type: str       # "cloudwatch_alarm" | "do_health_check"
    file: str               # source file that triggered this monitor
    name: str               # alarm / check name
    config: dict            # full alarm / check config dict
    created: bool = False   # True only if actually provisioned via API


@dataclass
class MonitorGenerationResult:
    pr_number: int
    repo: str
    monitors: list[MonitorConfig] = field(default_factory=list)
    files_analyzed: int = 0
    monitors_created: int = 0
    coverage_ratio: float = 0.0
    dry_run: bool = True


def _parse_monitor_result(
    answer: str,
    pr_number: int,
    repo: str,
    dry_run: bool,
) -> MonitorGenerationResult:
    """Extract JSON summary from agent answer → MonitorGenerationResult."""
    match = re.search(r"\{.*\}", answer, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            raw_monitors = data.get("monitors", [])
            monitors: list[MonitorConfig] = []
            for m in raw_monitors:
                if isinstance(m, dict):
                    monitors.append(MonitorConfig(
                        monitor_type=m.get("monitor_type", "cloudwatch_alarm"),
                        file=m.get("file", ""),
                        name=m.get("name", ""),
                        config=m.get("config", {}),
                        created=bool(m.get("created", False)),
                    ))
            return MonitorGenerationResult(
                pr_number=pr_number,
                repo=repo,
                monitors=monitors,
                files_analyzed=int(data.get("files_analyzed", 0)),
                monitors_created=int(data.get("monitors_created", len(monitors))),
                coverage_ratio=float(data.get("coverage_ratio", 0.0)),
                dry_run=dry_run,
            )
        except (json.JSONDecodeError, ValueError, TypeError, KeyError):
            pass

    logger.warning(
        "[MonitorGen] Non-JSON answer from agent — returning empty result. Raw: %s",
        answer[:300],
    )
    return MonitorGenerationResult(pr_number=pr_number, repo=repo, dry_run=dry_run)


# ---------------------------------------------------------------------------
# MonitorGenerationAgent
# ---------------------------------------------------------------------------

class MonitorGenerationAgent(BaseAgent):
    """
    Classifies a merged PR diff and generates monitoring coverage configs.

    Uses Claude Haiku — pattern recognition over diffs, no deep reasoning required.

    Usage:
        agent = MonitorGenerationAgent()
        result = await agent.generate_monitors("org", "repo", 42, "fix: patch S3 handler")
        print(result.monitors_created, "monitors generated")
    """

    def __init__(
        self,
        github: GitHubService | None = None,
        llm: LLMService | None = None,
    ) -> None:
        super().__init__(llm=llm or LLMService(model=HAIKU_MODEL))
        self._dry_run = not settings.create_monitors
        try:
            self._github = github or GitHubService()
        except ValueError:
            # No GitHub token — tools return helpful error strings instead of crashing
            self._github = None  # type: ignore[assignment]
        self._register_tools()

    def _register_tools(self) -> None:
        github = self._github
        dry_run = self._dry_run

        async def _analyze_pr_diff(owner: str, repo: str, pr_number: int) -> str:
            """Fetch PR diff and return a structured summary of changed files."""
            if github is None:
                return "Error: GitHub token not configured. Set GITHUB_TOKEN in .env."
            try:
                pr = await github.get_pr(owner, repo, pr_number)
                files = await github.get_pr_diff(owner, repo, pr_number)
            except GitHubError as exc:
                return f"GitHub API error: {exc}"

            if not files:
                return "No changed files found in this PR."

            lines = [
                f"PR #{pr.number}: {pr.title}",
                f"Branch: {pr.head_branch} → {pr.base_branch}",
                f"Files changed: {len(files)}",
                "",
            ]
            for f in files:
                new_syms: list[str] = []
                if f.patch:
                    added_text = "\n".join(
                        ln[1:] for ln in f.patch.splitlines() if ln.startswith("+")
                    )
                    for pat in _FN_PATTERNS:
                        new_syms.extend(pat.findall(added_text))
                    # Deduplicate, cap at 10
                    seen: set[str] = set()
                    deduped: list[str] = []
                    for s in new_syms:
                        if s not in seen:
                            seen.add(s)
                            deduped.append(s)
                    new_syms = deduped[:10]

                sym_str = f" | new: {', '.join(new_syms[:5])}" if new_syms else ""
                lines.append(
                    f"  {f.filename} | +{f.additions} -{f.deletions} | status={f.status}{sym_str}"
                )
            return "\n".join(lines)

        async def _generate_cloudwatch_alarms(
            file: str,
            additions: int,
            new_functions: list | str = "",
            service_name: str = "",
        ) -> str:
            """Generate CloudWatch alarm configs for a changed file."""
            alarm_count = max(1, int(additions) // 75)
            ext = file.rsplit(".", 1)[-1].lower() if "." in file else ""

            # Choose CloudWatch namespace by file type / path hints
            # Path hints take priority over extension (a route file is an API endpoint)
            if any(tok in file.lower() for tok in ("route", "api", "handler", "controller")):
                namespace, metric = "AWS/ApiGateway", "5XXError"
            elif ext in ("js", "ts", "mjs", "cjs"):
                namespace, metric = "AWS/Lambda", "Errors"
            else:
                namespace, metric = "AWS/ECS", "CPUUtilization"

            base_name = re.sub(r"[^a-zA-Z0-9-]", "_", file)[:30]
            svc = (service_name or base_name).strip("_")

            alarms = []
            for i in range(alarm_count):
                suffix = f"_{i + 1}" if alarm_count > 1 else ""
                alarms.append({
                    "AlarmName": f"auto-{svc}-{metric.lower()}{suffix}",
                    "MetricName": metric,
                    "Namespace": namespace,
                    "Statistic": "Sum",
                    "Period": 300,
                    "EvaluationPeriods": 1,
                    "Threshold": 1,
                    "ComparisonOperator": "GreaterThanOrEqualToThreshold",
                    "AlarmActions": [],
                    "Dimensions": [{"Name": "ServiceName", "Value": svc}],
                })
            return json.dumps(alarms, indent=2)

        async def _generate_do_health_checks(
            file: str,
            additions: int,
            new_endpoints: list | str = "",
        ) -> str:
            """Generate DigitalOcean health check configs for new endpoints."""
            endpoints: list[str] = (
                new_endpoints if isinstance(new_endpoints, list) else []
            )
            if not endpoints:
                endpoints = ["/health"]

            checks = []
            for endpoint in endpoints[:3]:  # cap at 3 per file
                path = endpoint if endpoint.startswith("/") else f"/{endpoint}"
                checks.append({
                    "protocol": "HTTP",
                    "port": 80,
                    "path": path,
                    "check_interval_seconds": 10,
                    "response_timeout_seconds": 5,
                    "unhealthy_threshold": 3,
                    "healthy_threshold": 2,
                })
            return json.dumps(checks, indent=2)

        async def _create_cloudwatch_alarm(alarm_config: dict | str) -> str:
            """Provision a CloudWatch alarm. Dry-run by default."""
            if dry_run:
                cfg = alarm_config if isinstance(alarm_config, dict) else {}
                name = cfg.get("AlarmName", "unnamed")
                return (
                    f"DRY_RUN: alarm '{name}' config logged, not created. "
                    "Set CREATE_MONITORS=true in .env to provision."
                )
            try:
                import asyncio

                import boto3
                cw = boto3.client("cloudwatch")
                cfg = alarm_config if isinstance(alarm_config, dict) else json.loads(str(alarm_config))
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: cw.put_metric_alarm(**cfg)
                )
                return f"Created CloudWatch alarm: {cfg.get('AlarmName', 'unnamed')}"
            except Exception as exc:
                return f"Error creating alarm: {exc}"

        self.register_tool(
            "analyze_pr_diff",
            _analyze_pr_diff,
            (
                "Fetch a merged PR diff and return changed files with additions, deletions, "
                "and detected new functions / endpoints. "
                "Input: {owner: string, repo: string, pr_number: integer}"
            ),
        )
        self.register_tool(
            "generate_cloudwatch_alarms",
            _generate_cloudwatch_alarms,
            (
                "Generate CloudWatch alarm configs for a changed file. "
                "Coverage target: 1 alarm per 75 lines of additions (Ramp standard). "
                "Input: {file: string, additions: integer, new_functions: list[string], service_name: string}"
            ),
        )
        self.register_tool(
            "generate_do_health_checks",
            _generate_do_health_checks,
            (
                "Generate DigitalOcean health check configs for new endpoints found in a changed file. "
                "Input: {file: string, additions: integer, new_endpoints: list[string]}"
            ),
        )
        self.register_tool(
            "create_cloudwatch_alarm",
            _create_cloudwatch_alarm,
            (
                "Provision a CloudWatch alarm from a config dict. "
                "Dry-run by default — set CREATE_MONITORS=true in .env to actually create. "
                "Input: {alarm_config: object}"
            ),
        )

    async def generate_monitors(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        pr_title: str = "",
        pr_description: str = "",
    ) -> MonitorGenerationResult:
        """
        Generate monitoring coverage for a merged PR.

        Args:
            owner:          GitHub org or user
            repo:           Repository name (without owner)
            pr_number:      Merged PR number
            pr_title:       PR title (used in prompt context)
            pr_description: PR body / description (first 200 chars used)

        Returns:
            MonitorGenerationResult with generated monitor configs and coverage metrics.
        """
        desc_snippet = (pr_description or "")[:200]
        prompt = f"""You are a monitoring coverage agent. A PR was just merged.

Repo: {owner}/{repo}
PR #{pr_number}: {pr_title}
Description: {desc_snippet}

STEPS:
1. Call analyze_pr_diff with owner="{owner}", repo="{repo}", pr_number={pr_number}
2. For each file with additions > 0:
   a. Call generate_cloudwatch_alarms with file, additions, any new_functions found, service_name="{repo}"
   b. If the file name contains "route", "api", "handler", "endpoint", or "controller",
      also call generate_do_health_checks with file, additions, any new endpoint paths found
3. Answer with ONLY a valid JSON object (no other text):

MANDATORY CONSTRAINTS:
- You MUST call analyze_pr_diff first, even if the PR title or description is empty.
- Do not output an Answer until analyze_pr_diff has been called and its result observed.
- If analyze_pr_diff returns an error or empty diff, output the JSON with monitors=[] and files_analyzed=0.

{{
  "monitors": [
    {{
      "monitor_type": "cloudwatch_alarm",
      "file": "path/to/file.js",
      "name": "auto-service-errors",
      "config": {{}},
      "created": false
    }}
  ],
  "files_analyzed": <integer>,
  "monitors_created": <integer>,
  "coverage_ratio": <float between 0.0 and 1.0>
}}

coverage_ratio = monitors_generated / max(1, total_additions / 75)
"""
        result = await self.run(prompt)
        return _parse_monitor_result(result.answer, pr_number, repo, self._dry_run)
