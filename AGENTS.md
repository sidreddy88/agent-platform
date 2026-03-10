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
