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
