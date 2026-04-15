"""
IncidentResponseAgent — auto-diagnoses production incidents and recommends actions.

Flow (driven by ReAct loop in BaseAgent):
  1. gather_context         → pull logs, metrics, and ECS health around incident time
  2. search_logs            → find error patterns across log groups
  3. check_recent_deployments → correlate incident with recent deploys
  4. search_codebase        → find relevant code via RAG (optional)
  5. search_similar_incidents → surface past incidents with matching symptoms
  6. generate_diagnosis     → structured root cause + evidence + actions

Key principle:
  The agent can RECOMMEND any action, but actions are risk-rated:
    LOW      — safe to run immediately (e.g. read logs, view metrics)
    MEDIUM   — reversible, run with awareness (e.g. restart a single task)
    HIGH     — significant impact, requires human review before executing
    CRITICAL — irreversible or wide blast radius — MUST have human approval
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any

from app.agents.base import AgentResult, BaseAgent
from app.core.config import settings
from app.services.approvals import ApprovalService, approval_service as _default_approval_svc
from app.services.aws import AWSError, AWSService
from app.services.llm import LLMService
from app.services.rag import RAGService


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Action:
    description: str
    risk: str           # LOW / MEDIUM / HIGH / CRITICAL
    requires_approval: bool
    command: str | None = None   # concrete command / API call if applicable


@dataclass
class Diagnosis:
    root_cause: str
    confidence: str         # HIGH / MEDIUM / LOW
    affected_services: list[str]
    evidence: list[str]
    actions: list[Action]
    deployment_correlated: bool
    raw_llm: str            # full LLM output for traceability


# ---------------------------------------------------------------------------
# Risk helpers
# ---------------------------------------------------------------------------

_HIGH_RISK_ACTIONS = re.compile(
    r"(?i)(rollback|redeploy|restart\s+service|scale\s+down|terminate\s+instance"
    r"|delete|drop\s+table|flush\s+cache|disable\s+feature\s+flag"
    r"|kill\s+process|force\s+stop|deregister)",
    re.DOTALL,
)

_CRITICAL_RISK_ACTIONS = re.compile(
    r"(?i)(drop\s+database|destroy|nuke|wipe|truncate\s+table"
    r"|delete\s+cluster|remove\s+all|purge)",
    re.DOTALL,
)


def _rate_action_risk(description: str) -> tuple[str, bool]:
    """Return (risk_level, requires_approval)."""
    if _CRITICAL_RISK_ACTIONS.search(description):
        return "CRITICAL", True
    if _HIGH_RISK_ACTIONS.search(description):
        return "HIGH", True
    if re.search(r"(?i)(increase|scale\s+up|add\s+capacity|enable|update\s+config)", description):
        return "MEDIUM", False
    return "LOW", False


# ---------------------------------------------------------------------------
# Past incidents (placeholder — replace with real DB / vector search)
# ---------------------------------------------------------------------------

_KNOWN_INCIDENTS: list[dict] = [
    {
        "id": "INC-001",
        "title": "ECS service task crash-loop due to OOMKilled",
        "symptoms": ["tasks failing to start", "OOMKilled", "exit code 137", "memory"],
        "root_cause": "Container memory limit too low for workload spike",
        "resolution": "Increased task memory from 512MB to 1024MB and redeployed",
        "duration_minutes": 25,
    },
    {
        "id": "INC-002",
        "title": "Spike in 500 errors after deployment",
        "symptoms": ["500 errors", "error spike", "deployment", "null pointer", "undefined"],
        "root_cause": "New deployment introduced null reference bug in payment service",
        "resolution": "Rolled back deployment within 8 minutes",
        "duration_minutes": 12,
    },
    {
        "id": "INC-003",
        "title": "Database connection pool exhaustion",
        "symptoms": ["connection pool", "timeout", "database", "too many connections", "ECONNREFUSED"],
        "root_cause": "Connection leak introduced in ORM query refactor",
        "resolution": "Deployed hotfix to close connections, restarted service",
        "duration_minutes": 45,
    },
    {
        "id": "INC-004",
        "title": "High CPU causing latency degradation",
        "symptoms": ["high cpu", "slow response", "latency", "timeout", "p99"],
        "root_cause": "Inefficient query missing index, triggered by traffic spike",
        "resolution": "Added database index, CPU normalized within 5 minutes",
        "duration_minutes": 30,
    },
    {
        "id": "INC-005",
        "title": "Dependency service outage causing cascading failures",
        "symptoms": ["upstream", "dependency", "connection refused", "circuit breaker", "503"],
        "root_cause": "Third-party API rate limit hit due to retry storm",
        "resolution": "Enabled circuit breaker, added exponential backoff",
        "duration_minutes": 60,
    },
]


def _find_similar_incidents(symptoms: str) -> list[dict]:
    """Simple keyword match against known incidents. Replace with vector search."""
    symptom_words = set(symptoms.lower().split())
    scored = []
    for inc in _KNOWN_INCIDENTS:
        inc_words = set(" ".join(inc["symptoms"]).lower().split())
        overlap = len(symptom_words & inc_words)
        if overlap > 0:
            scored.append((overlap, inc))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [inc for _, inc in scored[:3]]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def gather_context(
    service: str,
    time_window: int,
    aws: AWSService,
    cluster: str | None = None,
    log_group: str | None = None,
) -> str:
    cluster = cluster or settings.ecs_cluster or "default"
    """Pull ECS health, CloudWatch metrics, and logs around the incident time."""
    parts: list[str] = [f"=== Context for service '{service}' (last {time_window} min) ===\n"]

    # ECS health
    try:
        ecs = aws.get_ecs_status(cluster, service)
        task_failures = aws.get_ecs_task_failures(cluster, service)
        lines = [
            f"ECS  running/desired: {ecs.running_count}/{ecs.desired_count}",
            f"     deployment: {ecs.deployment_status}",
            f"     pending: {ecs.pending_count}",
        ]
        if ecs.events:
            lines.append("     events:")
            for e in ecs.events[:3]:
                lines.append(f"       - {e}")
        if task_failures:
            lines.append(f"     stopped tasks ({len(task_failures)}):")
            for f in task_failures[:3]:
                lines.append(f"       [{f['task_id']}] {f['stopped_reason']}")
                for cr in f["container_reasons"]:
                    lines.append(f"         container={cr['name']} exit={cr['exit_code']} {cr['reason']}")
        parts.append("\n".join(lines))
    except AWSError as exc:
        parts.append(f"ECS: could not fetch — {exc}")

    # CloudWatch metrics: CPU and MemoryUtilization
    for metric_name in ("CPUUtilization", "MemoryUtilization"):
        try:
            m = aws.get_metrics(
                namespace="AWS/ECS",
                metric_name=metric_name,
                dimensions={"ClusterName": cluster, "ServiceName": service},
                minutes=time_window,
                period=60,
            )
            if m.datapoints:
                parts.append(
                    f"{metric_name}: avg={m.average}%  max={m.maximum}%  "
                    f"({len(m.datapoints)} datapoints)"
                )
        except AWSError:
            pass

    # Logs
    if log_group:
        try:
            summary = aws.get_service_logs(log_group, minutes=time_window)
            parts.append(
                f"Logs [{log_group}]: {summary.total_events} events, "
                f"{summary.error_count} errors, {summary.warning_count} warnings"
            )
            if summary.recent_errors:
                parts.append("  Recent errors:")
                for e in summary.recent_errors[-5:]:
                    parts.append(f"    {e[:200]}")
        except AWSError as exc:
            parts.append(f"Logs: could not fetch — {exc}")

    return "\n\n".join(parts)


async def search_logs(
    query: str,
    log_groups: list[str],
    aws: AWSService,
    minutes: int = 30,
) -> str:
    """Search for an error pattern across multiple CloudWatch log groups."""
    pattern = re.compile(query, re.IGNORECASE)
    results: list[str] = []

    for lg in log_groups:
        try:
            summary = aws.get_service_logs(lg, minutes=minutes)
            matches = [
                line for line in summary.sample_lines + summary.recent_errors
                if pattern.search(line)
            ]
            if matches:
                results.append(f"[{lg}] {len(matches)} match(es):")
                for m in matches[:5]:
                    results.append(f"  {m[:200]}")
        except AWSError as exc:
            results.append(f"[{lg}] error: {exc}")

    return "\n".join(results) if results else f"No matches for '{query}' in the last {minutes} min."


async def check_recent_deployments(
    services: list[str],
    aws: AWSService,
    hours: int = 6,
    cluster: str | None = None,
) -> str:
    cluster = cluster or settings.ecs_cluster or "default"
    """Check for deployments in the last N hours and flag any that are degraded."""
    lines: list[str] = [f"Deployments in the last {hours}h:\n"]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    for svc in services:
        try:
            status = aws.get_ecs_status(cluster, svc)
            recent = []
            for d in status.deployments:
                created_str = d.get("created_at", "")
                try:
                    created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    if created >= cutoff:
                        recent.append(d)
                except (ValueError, AttributeError):
                    recent.append(d)  # include if we can't parse the date

            if recent:
                lines.append(f"  {svc}:")
                for d in recent:
                    flag = " ⚠ POSSIBLE CAUSE" if status.deployment_status == "degraded" else ""
                    lines.append(
                        f"    [{d['id']}] {d['status']} rollout={d['rollout']} "
                        f"running={d['running']}/{d['desired']}{flag}"
                    )
            else:
                lines.append(f"  {svc}: no recent deployments")
        except AWSError as exc:
            lines.append(f"  {svc}: error — {exc}")

    return "\n".join(lines)


async def search_codebase(query: str, rag: RAGService | None) -> str:
    """Search the indexed codebase for code relevant to the incident."""
    if rag is None:
        return "RAG not configured — codebase search unavailable."
    try:
        chunks = await rag.search(query, n_results=4)
    except Exception as exc:
        return f"RAG search error: {exc}"

    if not chunks:
        return "No relevant code found."

    parts = []
    for c in chunks:
        parts.append(
            f"--- {c.file_path}:{c.start_line}-{c.end_line} (score={c.score}) ---\n"
            f"{c.content[:400]}"
        )
    return "\n\n".join(parts)


async def search_similar_incidents(symptoms: str) -> str:
    """Find past incidents with similar symptoms from the knowledge base."""
    matches = _find_similar_incidents(symptoms)
    if not matches:
        return "No similar past incidents found."

    lines = ["Similar past incidents:\n"]
    for inc in matches:
        lines.append(
            f"  {inc['id']}: {inc['title']}\n"
            f"    Root cause : {inc['root_cause']}\n"
            f"    Resolution : {inc['resolution']}\n"
            f"    Duration   : {inc['duration_minutes']} min\n"
        )
    return "\n".join(lines)


async def generate_diagnosis(
    context: str,
    llm: LLMService,
) -> str:
    """
    Produce a structured root cause analysis with risk-rated recommended actions.
    `context` should be a concatenation of all gathered evidence.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    prompt = f"""You are a senior SRE performing root cause analysis for a production incident.

EVIDENCE GATHERED:
{context}

Produce a structured diagnosis in EXACTLY this format:

# Incident Diagnosis
**Analyzed:** {now}

## Root Cause
<1-3 sentences stating the most likely root cause. Be specific — name the service, error type, and mechanism.>

**Confidence:** <HIGH / MEDIUM / LOW>
**Reason for confidence:** <one sentence explaining why>

## Affected Services
- <service name and how it's affected>

## Evidence
- <specific fact from the data that supports the root cause>
- <another fact — tie each bullet to a concrete observation>
- <add as many as relevant>

## Deployment Correlation
<YES / NO / POSSIBLE> — <one sentence explaining the connection or lack thereof>

## Recommended Actions

### Immediate (run now — LOW risk)
- [ ] <action> — <why and expected effect>

### Short-term (with awareness — MEDIUM risk)
- [ ] <action> — <why and expected effect>

### Requires Human Approval (HIGH risk)
⚠ The following actions have significant impact and MUST be reviewed before executing:
- [ ] <action> — <why, expected effect, and rollback plan>

### Critical — Human Approval Required (CRITICAL risk)
🚨 Do NOT execute without explicit sign-off:
- [ ] <action> — <why, blast radius, and rollback plan>
(Write "None" if no critical actions needed)

## Timeline Hypothesis
<2-3 sentences describing the likely sequence of events that led to the incident>

## Prevention
- <concrete change to prevent recurrence>
- <monitoring/alerting improvement to detect this earlier>

Be direct. An on-call engineer should be able to act on this in under 2 minutes."""

    return await llm.complete(
        messages=[{"role": "user", "content": prompt}],
        system=(
            "You are a senior SRE performing root cause analysis. "
            "Be precise, evidence-based, and conservative about risk ratings. "
            "When in doubt, rate actions higher risk, not lower."
        ),
    )


# ---------------------------------------------------------------------------
# IncidentResponseAgent
# ---------------------------------------------------------------------------

class IncidentResponseAgent(BaseAgent):
    """
    Auto-diagnoses production incidents and recommends risk-rated actions.

    Usage:
        agent = IncidentResponseAgent()
        result = await agent.run(json.dumps({
            "alert": "ECS service 'api' has 0/3 tasks running",
            "service": "api",
            "cluster": "prod",
            "log_group": "/app/prod/api",
            "time_window": 30,
        }))
        print(result.answer)

    Input JSON fields:
        alert       - the incident alert or description (required)
        service     - primary ECS service name
        cluster     - ECS cluster (default: "default")
        log_group   - CloudWatch log group to search
        log_groups  - list of log groups for cross-service search
        time_window - minutes of history to examine (default: 30)
        hours       - hours back to check for deployments (default: 6)

    Risk levels:
        LOW      — safe to run immediately
        MEDIUM   — reversible, run with awareness
        HIGH     — requires human review before executing
        CRITICAL — irreversible / wide blast radius — MUST have human approval
    """

    def __init__(
        self,
        aws: AWSService | None = None,
        rag: RAGService | None = None,
        approvals: ApprovalService | None = None,
    ) -> None:
        super().__init__()
        self._aws = aws or AWSService()
        self._rag = rag
        self._approvals = approvals or _default_approval_svc
        self._register_tools()

    def _register_tools(self) -> None:
        aws = self._aws
        rag = self._rag
        llm = self._llm

        # Accumulate all gathered evidence for the final diagnosis call
        self._evidence: list[str] = []

        _default_cluster = settings.ecs_cluster or "default"
        _default_log_groups = [g.strip() for g in settings.ecs_log_groups.split(",") if g.strip()]

        async def _gather_context(
            service: str,
            time_window: int = 30,
            cluster: str = "",
            log_group: str = "",
        ) -> str:
            resolved_cluster = cluster or _default_cluster
            resolved_log_group = log_group or (_default_log_groups[0] if _default_log_groups else "")
            out = await gather_context(service, time_window, aws, resolved_cluster, resolved_log_group or None)
            self._evidence.append(out)
            return out

        async def _search_logs(
            query: str,
            log_groups: list[str],
            minutes: int = 30,
        ) -> str:
            out = await search_logs(query, log_groups, aws, minutes)
            self._evidence.append(f"Log search '{query}':\n{out}")
            return out

        async def _check_recent_deployments(
            services: list[str],
            hours: int = 6,
            cluster: str = "",
        ) -> str:
            out = await check_recent_deployments(services, aws, hours, cluster or _default_cluster)
            self._evidence.append(out)
            return out

        async def _search_codebase(query: str) -> str:
            return await search_codebase(query, rag)

        async def _search_similar_incidents(symptoms: str) -> str:
            return await search_similar_incidents(symptoms)

        async def _generate_diagnosis(context: str = "") -> str:
            # Merge all accumulated evidence with any extra context passed explicitly
            all_evidence = "\n\n---\n\n".join(self._evidence)
            if context:
                all_evidence = f"{all_evidence}\n\n---\n\nAdditional context:\n{context}"
            return await generate_diagnosis(all_evidence, llm)

        approvals = self._approvals

        async def _request_action_approval(
            action: str,
            description: str,
            risk_level: str,
            parameters: dict | None = None,
        ) -> str:
            """Submit a high-risk action for human approval."""
            req = await approvals.request_approval(
                agent_name="IncidentResponseAgent",
                action=action,
                parameters=parameters or {},
                risk_level=risk_level,
                description=description,
            )
            status = req.status.value
            if status in ("approved", "auto_approved"):
                return (
                    f"Action '{action}' was {status} (request ID: {req.id}). "
                    "You may proceed with executing it."
                )
            return (
                f"Action '{action}' requires human approval before it can be executed.\n"
                f"Approval request ID: {req.id}\n"
                f"Risk level: {req.risk_level.upper()}\n"
                f"What will happen: {req.description}\n"
                f"Status: PENDING — awaiting human decision\n"
                f"Approvers can visit: POST /approvals/{req.id}/approve"
            )

        _cluster_hint = f"default cluster: '{_default_cluster}'"
        _log_hint = (
            f"known log groups: {_default_log_groups}" if _default_log_groups
            else "no log groups configured — ask the user or omit log_group"
        )
        self.register_tool(
            "gather_context",
            _gather_context,
            (
                "Pull ECS health, CPU/memory metrics, and recent logs for a service "
                "around the incident time window. Always call this first. "
                f"Input: {{service: string, time_window: integer (minutes, default 30), "
                f"cluster: string ({_cluster_hint}), log_group: string ({_log_hint})}}"
            ),
        )
        self.register_tool(
            "search_logs",
            _search_logs,
            (
                "Search for a specific error pattern across one or more CloudWatch log groups. "
                "Use regex patterns. Good for finding stack traces, specific error codes, "
                f"or exception types across multiple services. {_log_hint}. "
                "Input: {query: string (regex), log_groups: [string], minutes: integer (default 30)}"
            ),
        )
        self.register_tool(
            "check_recent_deployments",
            _check_recent_deployments,
            (
                "Check for recent ECS deployments across services and flag any that "
                f"coincide with the incident window. Key for deployment correlation. "
                f"Input: {{services: [string], hours: integer (default 6), cluster: string ({_cluster_hint})}}"
            ),
        )
        self.register_tool(
            "search_codebase",
            _search_codebase,
            (
                "Search the indexed codebase for code relevant to the incident symptoms. "
                "Useful for understanding the blast radius or finding the buggy code path. "
                "Input: {query: string}"
            ),
        )
        self.register_tool(
            "search_similar_incidents",
            _search_similar_incidents,
            (
                "Search the incident knowledge base for past incidents with similar symptoms. "
                "Returns root cause and resolution from past incidents. "
                "Input: {symptoms: string (space-separated keywords)}"
            ),
        )
        self.register_tool(
            "generate_diagnosis",
            _generate_diagnosis,
            (
                "Generate a structured root cause analysis from all gathered evidence. "
                "Call this LAST after all context has been gathered. "
                "Produces: root cause + confidence, evidence list, affected services, "
                "risk-rated recommended actions (LOW/MEDIUM/HIGH/CRITICAL). "
                "HIGH and CRITICAL actions are flagged for human approval. "
                "Input: {context: string (optional extra context to include)}"
            ),
        )
        self.register_tool(
            "request_action_approval",
            _request_action_approval,
            (
                "Submit a HIGH or CRITICAL risk action for human approval before executing it. "
                "LOW and MEDIUM actions are auto-approved immediately. "
                "Returns the approval request ID and status. "
                "If PENDING, include the request ID in your final answer so the on-call "
                "engineer knows what to approve. "
                "Input: {action: string, description: string, risk_level: string, "
                "parameters: dict (optional)}"
            ),
        )

    async def run(self, user_input: str) -> AgentResult:
        """
        Run the incident response agent.

        user_input can be JSON:
            {
              "alert": "ECS api service 0/3 tasks running",
              "service": "api",
              "cluster": "prod",
              "log_group": "/app/prod/api",
              "time_window": 30
            }
        or plain text:
            "api service is down, 0 tasks running in prod cluster"
        """
        # Reset evidence accumulator for each new incident
        self._evidence = []

        try:
            params = json.loads(user_input)
            alert = params.get("alert", user_input)
            service = params.get("service", "")
            cluster = params.get("cluster", settings.ecs_cluster or "default")
            log_group = params.get("log_group", "")
            _cfg_log_groups = [g.strip() for g in settings.ecs_log_groups.split(",") if g.strip()]
            log_groups = params.get("log_groups", [log_group] if log_group else _cfg_log_groups)
            time_window = params.get("time_window", 30)
            hours = params.get("hours", 6)

            service_hint = f" Primary service: {service} in cluster {cluster}." if service else ""
            logs_hint = f" Log groups: {log_groups}." if log_groups else ""

            prompt = (
                f"INCIDENT ALERT: {alert}\n"
                f"{service_hint}{logs_hint}\n\n"
                "Diagnose this incident systematically:\n"
                f"1. gather_context for the affected service (time_window={time_window}min)\n"
                f"2. check_recent_deployments to see if a deploy triggered this\n"
                "3. search_logs for the specific error patterns you find\n"
                "4. search_similar_incidents with symptoms from what you've found\n"
                "5. search_codebase if you need to understand a specific code path\n"
                "6. generate_diagnosis with all gathered evidence\n"
                "7. For any HIGH or CRITICAL actions in the diagnosis, call "
                "request_action_approval for each one before including it in your answer\n\n"
                "MANDATORY CONSTRAINTS — these are hard rules, not suggestions:\n"
                "- You MUST call gather_context before any other tool. "
                "Never skip it, even if the alert text seems self-explanatory.\n"
                "- You MUST call generate_diagnosis before calling request_action_approval "
                "or writing your Answer. A diagnosis based only on the alert text is not valid.\n"
                "- Never call request_action_approval without first calling gather_context "
                "and generate_diagnosis. Doing so constitutes a hallucinated diagnosis.\n"
                "- Never say you will execute a HIGH or CRITICAL action directly. "
                "Always call request_action_approval first. Include the returned request ID "
                "in your final answer so the engineer knows what to approve."
            )
        except (json.JSONDecodeError, KeyError):
            prompt = (
                f"INCIDENT ALERT: {user_input}\n\n"
                "Diagnose this incident: gather context, check deployments, search logs, "
                "find similar past incidents, then generate a structured diagnosis. "
                "For any HIGH or CRITICAL actions, call request_action_approval before "
                "including them in your final answer. Include the request ID in the answer.\n\n"
                "MANDATORY CONSTRAINTS: You MUST call gather_context first, then "
                "generate_diagnosis, before calling request_action_approval or writing "
                "your Answer. Never skip evidence gathering."
            )

        return await super().run(prompt)
