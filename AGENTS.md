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
