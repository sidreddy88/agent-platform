# agent-platform

> Autonomous incident remediation for production systems. CloudWatch alarm to fix-PR in single-digit minutes — no human in the loop until approval.

**Live demo:** [app.remediatelabs.io](https://app.remediatelabs.io) · **Engineering blog:** [remediatelabs.io/blog](https://remediatelabs.io/blog) · **Architecture deep-dive:** [remediatelabs.io/projects/agent-platform](https://remediatelabs.io/projects/agent-platform)

---

## What it does

When a production error fires a CloudWatch alarm, the platform's pipeline:

1. **Detects** the alarm via SNS → HTTPS webhook (push-based; zero polling load on the production server).
2. **Triages** the event into real / noise / duplicate at P0–P3 severity using a Haiku-class classifier validated against a 100-case golden dataset.
3. **Diagnoses** the root cause with a Sonnet-class agent that grounds every claim against the actual repo via GitHub Code Search.
4. **Generates a fix** — writes a patch, runs it in a Docker sandbox against the real test suite, retries up to 3× on failure.
5. **Self-critiques** the fix, opens a GitHub PR, and queues the merge for human approval if HIGH/CRITICAL.

It is also a research substrate: every LLM call and tool execution is captured as a Langfuse span, every approval rejection feeds an RLHF preference dataset, and every resolved incident is auto-captured into the golden eval set.

---

## Headline numbers

| Metric | Value | Source |
|---|---|---|
| Time-to-first-PR | ~6 min from alarm | [`scripts/measure_mttr.py`](scripts/measure_mttr.py) |
| Triage accuracy | 92% | 100-case eval — `app/evals/golden_dataset.jsonl` |
| False-positive rate | < 8% | Triage decisions reviewed against ground truth |
| Sample size | 100+ production incidents | `agent_platform.db` from live deploy |

Numbers refresh by re-running `scripts/measure_mttr.py --since YYYY-MM-DD`. See [`docs/MANUAL_BASELINE.md`](docs/MANUAL_BASELINE.md) for the pre-agent baseline these are measured against.

---

## Architecture

```
CloudWatch alarm  ─►  SNS  ─►  /webhooks/cloudwatch-alarm
                                       │
                                       ▼
                              ┌────────────────┐
                              │  Dedup gate    │  string-match · SQL · RAG (live store lookup)
                              └────────┬───────┘
                                       │   (new event)
                                       ▼
                              ┌────────────────┐
                              │  TriageAgent   │  real / noise / duplicate · P0–P3
                              └────────┬───────┘
                                       │   (real, ≥ P2)
                                       ▼
                              ┌────────────────┐
                              │ DiagnosisAgent │  grounds every symbol against the live repo
                              └────────┬───────┘
                                       ▼
                              ┌────────────────┐
                              │FixGeneration   │  writes patch · sandbox test · retry 3×
                              └────────┬───────┘
                                       ▼
                              ┌────────────────┐
                              │ CodeReview     │  self-critique → open PR
                              └────────┬───────┘
                                       ▼
                              ┌────────────────┐
                              │ Approval gate  │  HIGH/CRITICAL → human · merge → MonitorGen
                              └────────────────┘
```

The pipeline runs on FastAPI with WebSocket streaming for the live dashboard. State lives in Postgres (`agent_runs`, `incidents`, `approvals`, `monitor_records`); RAG candidate matching lives in pgvector. The dashboard is a React + Vite bundle served from the same ECS container.

---

## Tech stack

| Layer | Choice |
|---|---|
| Agent runtime | Anthropic SDK · Claude Opus 4.6 / Sonnet 4.6 / Haiku 4.5 |
| Web framework | FastAPI · WebSocket streaming · Pydantic v2 |
| Storage | Postgres (SQLAlchemy Core) · pgvector for RAG |
| Sandbox | Docker Compose · Jest · mongodb-memory-server |
| Tracing | Langfuse — every LLM call + tool execution as nested spans |
| Resilience | Circuit breakers · schema validation at handoffs · context checkpointing |
| Deploy | AWS ECS Fargate · ALB · Cloudflare · GitHub Actions OIDC |
| Frontend | React + Vite · WebSocket dashboard · served from same container |

---

## Quick start

```bash
# 1. Install
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Environment
cp .env.example .env  # fill ANTHROPIC_API_KEY, GITHUB_TOKEN, AWS keys, etc.

# 3. Run
npm run dev           # FastAPI on :8000 + Vite on :5173

# 4. Tests (no live API calls — all mocked)
pytest tests/
```

Full agent docs: [`CLAUDE.md`](CLAUDE.md).

---

## Project structure

```
app/
  agents/          # BaseAgent + 5 specialised agents (triage, diagnosis, fix, review, monitor)
  api/routes/      # FastAPI route handlers
  core/config.py   # Settings via pydantic-settings
  models/          # Pydantic data models (ErrorEvent, IncidentState, etc.)
  services/        # LLM, GitHub, AWS, RAG, approvals, tracing, circuit_breaker, sandbox
mcp_server/        # MCP server exposing agents to Claude Desktop
infra/             # Terraform for ECS Fargate + Cloudflare + SNS
scripts/           # measure_mttr.py, eval_rag.py, triage_replay.py
targets/           # External codebases the platform operates on
tests/             # pytest test suite (~600 tests, all mocked)
docs/              # MANUAL_BASELINE, architecture notes
```

---

## Design decisions worth defending

**Push, not poll.** CloudWatch alarms → SNS → HTTPS webhook. Zero polling load on the production server. Detection latency drops from ~7d (human attention) to single-digit minutes.

**RAG finds candidates. The live store confirms truth.** ChromaDB / pgvector hold index-time snapshots; current incident state lives in Postgres. Blocking decisions always re-read the live store. ([why](https://remediatelabs.io/blog/live-store-vs-chromadb))

**Hard blocks are deterministic, soft hints are LLM-shaped.** Dedup is a hard block (drop the event); regression context is a soft hint (prompt injection). Mixing the two created a four-failure-mode bug. ([walked through here](https://remediatelabs.io/blog/rag-dedup-failure))

**Ground every symbol against the repo.** DiagnosisAgent runs `verify_symbol_in_repo` via GitHub Code Search; a server-side guard re-checks every named function in the parsed output and rejects fabricated camelCase identifiers.

**Sandbox before PR.** Every fix runs in a Docker container against the real test suite. If tests fail, regenerate up to 3× before opening any GitHub noise.

**Approval gate for HIGH/CRITICAL.** Configurable risk threshold; rejections are logged as RLHF preference pairs.

---

## Engineering blog

Notes from building this — debugging stories, architecture posts, retrieval design:

- [The Three Camps of Retrieval Architecture](https://remediatelabs.io/blog/retrieval-architectures) — Improved RAG vs GraphRAG vs Ragless, when to reach for each
- [Why the Same Bug Kept Creating New Incidents](https://remediatelabs.io/blog/rag-dedup-failure) — four-failure-mode dedup bug
- [RAG Finds the Candidate. The Live Store Confirms the Truth.](https://remediatelabs.io/blog/live-store-vs-chromadb) — search index vs source of truth
- [Why My AI Agent Kept Adding Null Checks Instead of Fixing the Bug](https://remediatelabs.io/blog/symptom-fix-antipattern) — producer/consumer routing
- [Not Every Agent Needs a ReAct Loop](https://remediatelabs.io/blog/not-every-agent-needs-react) + [Four Patterns for Structuring Agents](https://remediatelabs.io/blog/four-agent-patterns) — agent design patterns
- [My Agent Ran for 58 Seconds and Made Up Every Number](https://remediatelabs.io/blog/debugging-hallucinating-agents) + [The Fix That Broke My Agent in a Different Way](https://remediatelabs.io/blog/prompt-constraints-loop) — hallucination + loop debugging
- [Typed Boundaries Make Multi-Agent Systems Readable](https://remediatelabs.io/blog/typed-boundaries-agents) — where to put parsing logic

---

## Deployment

ECS Fargate behind a Cloudflare-fronted ALB. CI/CD via GitHub Actions OIDC (no secrets in repo). Terraform module in [`infra/agent_platform/`](infra/agent_platform/). On `main` push, the build, push to ECR, and ECS deploy run automatically.

---

## Author

Built by [Siddharth Sukumar](https://github.com/sidreddy88) · [sidreddy88@gmail.com](mailto:sidreddy88@gmail.com) · [remediatelabs.io](https://remediatelabs.io)
