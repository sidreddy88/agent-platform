# Agents

## BaseAgent
- **File:** `app/agents/base.py`
- **Type:** Base class (not used directly)
- **Description:** ReAct loop implementation (Thought → Action → Observation → Answer). Max 10 iterations.
- **Tools:** None built-in — tools are registered at runtime via `register_tool()` or `@agent.tool()`

## RequirementsAgent
- **File:** `app/agents/requirements.py`
- **Type:** Concrete agent (extends BaseAgent)
- **Description:** Converts a raw product requirement or ticket into a structured technical specification.
- **Tools:**
  - `analyze_requirement` — breaks down the requirement into components, edge cases, implicit requirements
  - `estimate_effort` — produces story points, time range, and per-area breakdown
  - `generate_spec` — assembles the final spec document (title, overview, functional reqs, API/DB changes, testing, risks, open questions)

## CodeReviewAgent
- **File:** `app/agents/code_review.py`
- **Type:** Concrete agent (extends BaseAgent)
- **Description:** Reviews a GitHub pull request — fetches the PR and diff, analyzes each changed file for bugs, security issues, performance problems, and testing gaps, then produces a structured review with an approval recommendation.
- **Dependencies:** `GitHubService` (`app/services/github.py`) — requires `GITHUB_TOKEN` in `.env`
- **Tools:**
  - `fetch_pr` — fetches PR metadata (title, description, author, branches) and full diff from GitHub; caches results for subsequent tool calls. Input: `{owner, repo, pr_number}`
  - `analyze_file` — deep-dive analysis of a single changed file's diff; checks for bugs, security vulnerabilities (SQLi, XSS, hardcoded secrets), performance issues (N+1, unnecessary loops), and testing gaps. Input: `{filename}`
  - `generate_review` — assembles the final structured review (summary, issues by severity, security/performance assessments, testing gaps, APPROVE / REQUEST_CHANGES / NEEDS_DISCUSSION recommendation) and optionally posts it back to the PR. Input: `{owner, repo, pr_number, file_analyses, post_to_github}`
- **Usage:**
  ```python
  agent = CodeReviewAgent()
  result = await agent.run('{"owner": "acme", "repo": "backend", "pr_number": 42}')
  # with auto-post: add "post_to_github": true
  ```

## CICDAgent
- **File:** `app/agents/cicd.py`
- **Type:** Concrete agent (extends BaseAgent)
- **Description:** Monitors GitHub Actions workflow runs, diagnoses CI/CD failures, and suggests concrete fixes. Detects failure types (test failure, build error, dependency issue, timeout, flaky test) using log analysis and optionally searches the codebase via RAG for relevant context.
- **Dependencies:**
  - `GitHubService` (`app/services/github.py`) — requires `GITHUB_TOKEN` in `.env`
  - `RAGService` (`app/services/rag.py`) — optional; enables codebase search. Requires `OPENAI_API_KEY` in `.env`
- **Failure types detected:** `TEST_FAILURE`, `BUILD_ERROR`, `DEPENDENCY`, `TIMEOUT`, `FLAKY_TEST`, `UNKNOWN`
- **Tools:**
  - `get_workflow_runs` — lists recent workflow runs with status, branch, and commit info. Input: `{owner, repo, limit}`
  - `get_run_logs` — fetches and caches logs for all failed jobs in a run (last 300 lines per job). Input: `{owner, repo, run_id}`
  - `analyze_failure` — classifies the failure type and extracts root cause, affected files, and a codebase search query. Input: `{run_id}`
  - `search_codebase` — RAG search over the indexed codebase using the query from `analyze_failure`. Input: `{query}`
  - `suggest_fix` — produces a structured fix report (what went wrong, exact code change, verification steps, prevention). Input: `{run_id, codebase_context}`
- **Usage:**
  ```python
  # GitHub only
  agent = CICDAgent()

  # GitHub + codebase search
  agent = CICDAgent(rag=RAGService())

  result = await agent.run('{"owner": "acme", "repo": "backend"}')
  # target a specific run:
  result = await agent.run('{"owner": "acme", "repo": "backend", "run_id": 12345678}')
  print(result.answer)
  ```

## DeploymentAgent
- **File:** `app/agents/deployment.py`
- **Type:** Concrete agent (extends BaseAgent)
- **Description:** Monitors AWS infrastructure health across ECS, EC2, and CloudWatch. Takes a natural language question or a structured list of resources, gathers health data from AWS, auto-detects anomalies, and produces a concise SRE-style health report with recommended actions.
- **Dependencies:** `AWSService` (`app/services/aws.py`) — uses local AWS credentials (`~/.aws/credentials`) by default. Optional overrides via `.env`: `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
- **Issues detected:** `TASK_FAILING`, `CAPACITY_ISSUE`, `HIGH_CPU`, `INSTANCE_IMPAIRED`, `INSTANCE_DOWN`, `ERROR_SPIKE`, `DEPLOYMENT_ISSUE`
- **Tools:**
  - `get_ecs_status` — task counts (running vs desired), deployment rollout state, stopped task failure reasons, last 5 service events. Input: `{cluster, service}`
  - `get_ec2_status` — instance state, type, IPs, CPU utilization (last 5 min from CloudWatch), system/instance status checks. Input: `{instance_id}`
  - `get_service_logs` — fetches recent CloudWatch log streams, counts errors/warnings, returns last 10 error lines. Input: `{log_group, minutes}`
  - `get_metrics` — generic CloudWatch metric datapoints (average + max) for any namespace. Works with ECS, EC2, ALB, RDS, and custom metrics. Input: `{namespace, metric_name, dimensions, minutes}`
  - `check_health` — full health sweep across a list of resources; auto-detects issues and produces a structured report (overall status, issues by severity, recommended actions). Input: `{resources: [{type, ...}]}`
- **Usage:**
  ```python
  agent = DeploymentAgent()

  # Natural language question
  result = await agent.run("Is the prod API service healthy?")

  # Structured resource sweep
  result = await agent.run(json.dumps({
      "question": "Check overall health",
      "resources": [
          {"type": "ecs",  "cluster": "prod", "service": "api"},
          {"type": "ec2",  "instance_id": "i-0abc123"},
          {"type": "logs", "log_group": "/app/prod", "minutes": 30},
      ]
  }))
  print(result.answer)
  ```

## IncidentResponseAgent
- **File:** `app/agents/incident.py`
- **Type:** Concrete agent (extends BaseAgent)
- **Description:** Auto-diagnoses production incidents by gathering AWS context, correlating with recent deployments, searching logs for error patterns, and surfacing similar past incidents. Produces a structured root cause analysis with risk-rated recommended actions. High and critical risk actions are explicitly flagged for human approval before execution.
- **Dependencies:**
  - `AWSService` (`app/services/aws.py`) — requires AWS credentials
  - `RAGService` (`app/services/rag.py`) — optional; enables codebase search
- **Risk levels for actions:** `LOW` (run now) → `MEDIUM` (run with awareness) → `HIGH` (human review required) → `CRITICAL` (explicit sign-off required)
- **Tools:**
  - `gather_context` — pulls ECS health, CPU/memory metrics, and log summary for the affected service around the incident time window. Always called first. Input: `{service, time_window, cluster, log_group}`
  - `search_logs` — regex search across multiple CloudWatch log groups to find specific error patterns, stack traces, or exception types. Input: `{query, log_groups, minutes}`
  - `check_recent_deployments` — checks ECS deployments across services in the last N hours and flags any that coincide with the incident window. Input: `{services, hours, cluster}`
  - `search_codebase` — RAG search for code relevant to the incident symptoms (optional). Input: `{query}`
  - `search_similar_incidents` — keyword search against a built-in incident knowledge base to surface past incidents with matching symptoms and their resolutions. Input: `{symptoms}`
  - `generate_diagnosis` — assembles all gathered evidence and produces a structured RCA: root cause + confidence, evidence list, affected services, deployment correlation, risk-rated actions, timeline hypothesis, and prevention steps. Input: `{context}`
  - `request_action_approval` — submits a HIGH or CRITICAL action to the `ApprovalService` before executing. LOW/MEDIUM are auto-approved. Returns the request ID and status; PENDING requests must be approved via the API before the action can run. Input: `{action, description, risk_level, parameters}`
- **Usage:**
  ```python
  agent = IncidentResponseAgent()                  # AWS only
  agent = IncidentResponseAgent(rag=RAGService())  # AWS + codebase search

  result = await agent.run(json.dumps({
      "alert": "ECS api service has 0/3 tasks running",
      "service": "api",
      "cluster": "prod",
      "log_group": "/app/prod/api",
      "time_window": 30,
  }))
  print(result.answer)
  ```

## ApprovalService
- **File:** `app/services/approvals.py`
- **Type:** Service (not an agent — used by agents and API routes)
- **Description:** Gates high-risk agent actions behind human confirmation. Agents call `request_approval()` before executing sensitive actions; LOW/MEDIUM are auto-approved instantly while HIGH/CRITICAL are queued as PENDING and printed to the console (Slack/email ready). Humans approve or reject via the REST API.
- **Risk rules:**
  | Level | Default behaviour | Example actions |
  |---|---|---|
  | `LOW` | Auto-approved | Read logs, fetch metrics, view status |
  | `MEDIUM` | Auto-approved (configurable) | Post comment, send notification |
  | `HIGH` | Requires human approval | Restart service, rollback deployment |
  | `CRITICAL` | Requires human approval | Delete data, modify prod DB, destroy cluster |
- **API endpoints** (`app/api/routes/approvals.py`):
  - `GET  /approvals/pending` — list all requests awaiting a decision
  - `GET  /approvals` — list all requests (any status), newest first
  - `GET  /approvals/{id}` — get a single request by ID
  - `POST /approvals/{id}/approve` — approve: `{"approver": "alice"}`
  - `POST /approvals/{id}/reject` — reject: `{"approver": "bob", "reason": "..."}`
- **Usage:**
  ```python
  from app.services.approvals import approval_service

  req = await approval_service.request_approval(
      agent_name="IncidentResponseAgent",
      action="restart_ecs_service",
      parameters={"cluster": "prod", "service": "api"},
      risk_level="high",
      description="Restart the ECS api service to recover from task crash-loop.",
  )

  if req.status in ("approved", "auto_approved"):
      # safe to execute
      ...
  else:
      print(f"Pending approval — request ID: {req.id}")
  ```
