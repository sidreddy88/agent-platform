# agent-platform

> Autonomous incident remediation for production systems. CloudWatch alarm to fix-PR in single-digit minutes — no human in the loop until approval.

**Engineering blog:** [remediatelabs.io/blog](https://remediatelabs.io/blog) · **Architecture deep-dive:** [remediatelabs.io/projects/agent-platform](https://remediatelabs.io/projects/agent-platform)

---

## What it does

When a production error fires a CloudWatch alarm, the platform's pipeline:

1. **Detects** the alarm via SNS → HTTPS webhook (push-based; zero polling load on the production server).
2. **Triages** the event into real / noise / duplicate at P0–P3 severity using a Haiku-class classifier validated against a 100-case golden dataset.
3. **Diagnoses** the root cause with a Sonnet-class agent that grounds structured fields (affected file/function, secondary fixes) against the actual repo via GitHub Code Search.
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
| Sample size | 7 incidents (current live DB row count) | Postgres `incidents` table, live deploy |

Numbers refresh by re-running `scripts/measure_mttr.py --since YYYY-MM-DD`. Triage accuracy is sourced from the checked-in eval dataset (`app/evals/golden_dataset.jsonl`); sample size is a live, mutable count from the deploy's Postgres `incidents` table (verified directly against production — not reproducible from a fresh clone, and drops when incidents are cleared/deduped). There's no manual pre-agent baseline to compare against yet — these numbers stand on their own until real "before" incident data exists.

---

## Architecture

```
CloudWatch alarm ─► SNS ─► /webhooks/cloudwatch-alarm ──┐
                                                         │
CloudWatch Logs ─► DetectionService (poll · 5 min) ──────┤
                                                         ▼
                                              ┌────────────────┐
                                              │  Dedup gate    │  string-match · SQL · RAG (live store lookup)
                                              └────────┬───────┘
                                                        │   (new event)
                                                        ▼
                                              ┌────────────────┐
                                              │  TriageAgent   │  real / noise / duplicate · P0–P3 (Haiku)
                                              └────────┬───────┘
                                                        │   (real, ≥ P2)
                                                        ▼
                                              ┌────────────────┐
                                              │ DiagnosisAgent │  grounds every symbol against the live repo (Sonnet)
                                              └────────┬───────┘
                                          (< 70% confidence, no file identified) ──► ErrorClarityAgent
                                                        │                            adds logging only — never fixes
                                                        │ (≥ 70% confidence)          the bug, scope enforced in code
                                                        ▼                            (not just by prompt) (Haiku)
                                              ┌────────────────┐
                                              │ FixGeneration  │  writes patch · sandbox test · retry 3× · self-critique (Sonnet + Haiku)
                                              └────────┬───────┘
                                                        ▼
                                                 Open GitHub PR  ◄── or from ErrorClarityAgent, when it finds exact code
                                                        │
                                                        ▼
                                              ┌────────────────┐
                                              │ CodeReviewAgent│  independent review, posts PR comment — applies fix
                                              │                │  vs. observability-specific criteria (GPT-4.1)
                                              └────────┬───────┘
                                                        ▼
                                              ┌────────────────┐
                                              │ Approval gate  │  HIGH/CRITICAL → human · merge → MonitorGen
                                              └────────────────┘
```

Two independent detection paths feed the same dedup gate: a push-based SNS webhook (near-zero latency) and a `DetectionService` background loop polling CloudWatch Logs every 5 minutes as a backstop — this polls AWS's own CloudWatch API, not the target application's servers, so it adds no load there. Self-critique (Haiku) runs *inside* `FixGenerationAgent`, before the PR exists; `CodeReviewAgent` (GPT-4.1, via `litellm`) is a separate agent that reviews and comments *after* the PR is already open — two distinct steps, not one.

When `DiagnosisAgent`'s confidence lands below the fix threshold with no file identified, `ErrorClarityAgent` (`app/agents/error_clarity.py`) runs instead of `FixGenerationAgent` — it adds a logging or error-handling line so the *next* occurrence is diagnosable, and explicitly does not attempt the fix itself. That boundary is enforced structurally, not just by prompt: its only path to committing code requires a proposed change to add a net-new logging/error-handling call, or it's rejected outright and routed to a text-only recommendation instead — a config change or behavior fix, even a correct one, can't get through. `CodeReviewAgent` reviews PRs from both agents, with different criteria depending on which one opened it (root-cause/symptom-fix checks for a `FixGenerationAgent` PR; secrets/PII-leakage and behavior-change checks for an `ErrorClarityAgent` one, since there's no "root cause" to check against for a change that isn't a fix).

The pipeline runs on FastAPI with WebSocket streaming for the live dashboard. State lives in Postgres (`agent_runs`, `incidents`, `approvals`, `monitor_records`); RAG candidate matching lives in pgvector. The dashboard is a React + Vite bundle served from the same ECS container.

---

## Tech stack

| Layer | Choice |
|---|---|
| Agent runtime | Anthropic SDK · Claude Sonnet 4.6 / Haiku 4.5 · GPT-4.1 (via litellm, `CodeReviewAgent` only) |
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
pip install -r requirements-dev.txt   # requirements.txt + pytest, for local dev
cd frontend && npm install && cd ..

# 2. Environment
cp .env.example .env  # fill ANTHROPIC_API_KEY, GITHUB_TOKEN, AWS keys, etc.

# 3. Run
npm run dev           # FastAPI on :8000 + Vite on :5173

# 4. Tests (mocked — no live API calls)
pytest tests/
```

Full agent docs: [`CLAUDE.md`](CLAUDE.md).

---

## Try it

Quick start above gets you a running dashboard with an empty incident feed —
here's how to see the pipeline actually do something, without needing AWS or
CloudWatch at all.

**1. Point it at a repo you control.** Not the real production target app —
any repo you have write access to, ideally a throwaway test repo with a real
bug planted in it.

```
# in .env
FIX_TARGET_REPO=your-username/your-test-repo
GITHUB_TOKEN=<a PAT with write access to that repo>
```

**2. Inject a synthetic incident.** `POST /incidents/trigger` bypasses
CloudWatch entirely — describe the bug you planted:

```bash
curl -X POST localhost:8000/incidents/trigger \
  -H "Content-Type: application/json" \
  -d '{
    "error_type": "TYPE_ERROR",
    "title": "Cannot read properties of undefined (reading foo)",
    "description": "TypeError at routes/api/example.js:42 — foo is undefined",
    "service": "your-test-repo"
  }'
```

**3. Watch the dashboard.** `TriageAgent` classifies it real/noise/duplicate;
`DiagnosisAgent` reads your actual repo via GitHub Code Search to find the
root cause. At ≥70% confidence, `FixGenerationAgent` opens a real PR against
your test repo. Below threshold, it lands in `AWAITING_APPROVAL` instead —
also a legitimate outcome, worth seeing either way.

---

## Project structure

```
app/
  agents/          # BaseAgent + 12 agents — 7 in the production pipeline (triage,
                   # diagnosis, fix, review, merge-decision, error-clarity, monitor-gen),
                   # 5 standalone/earlier-design — see CLAUDE.md's Agents table
  api/routes/      # FastAPI route handlers
  core/config.py   # Settings via pydantic-settings
  models/          # Pydantic data models (ErrorEvent, IncidentState, etc.)
  services/        # LLM, GitHub, AWS, RAG, approvals, tracing, circuit_breaker, sandbox
mcp_server/        # MCP server exposing agents to Claude Desktop
infra/             # Terraform for ECS Fargate + Cloudflare + SNS
scripts/           # measure_mttr.py, eval_rag.py, triage_replay.py
targets/           # Fetched from S3 at container startup (scripts/fetch_target_harness.py) —
                   # empty on a fresh clone, not committed to this repo
tests/             # pytest test suite (871 tests, mocked — no live API calls)
docs/              # architecture notes
```

---

## Design decisions worth defending

**Push, not poll.** CloudWatch alarms → SNS → HTTPS webhook. Zero polling load on the production server. Detection latency drops from ~7d (human attention) to single-digit minutes.

**RAG finds candidates. The live store confirms truth.** ChromaDB / pgvector hold index-time snapshots; current incident state lives in Postgres. Blocking decisions always re-read the live store. ([why](https://remediatelabs.io/blog/rag-index-vs-live-store))

**Hard blocks are deterministic, soft hints are LLM-shaped.** Dedup is a hard block (drop the event); regression context is a soft hint (prompt injection). Mixing the two created a four-failure-mode bug. ([walked through here](https://remediatelabs.io/blog/rag-dedup-failure))

**Ground every symbol against the repo.** DiagnosisAgent runs `verify_symbol_in_repo` via GitHub Code Search; a server-side guard re-checks every named function in the parsed output and rejects fabricated camelCase identifiers.

**Sandbox before PR.** Every fix runs in a Docker container against the real test suite. If tests fail, regenerate up to 3× before opening any GitHub noise.

**Scope enforced in code, not by prompt.** `ErrorClarityAgent`'s prompt already says "add visibility, don't fix the bug" — that alone didn't stop it from once suppressing a warning via a schema-option change with zero logging added. Its commit path now requires a net-new logging/error-handling call in the proposed diff; anything else is rejected regardless of how the model justifies it.

**Approval gate for HIGH/CRITICAL.** Configurable risk threshold; rejections are logged as RLHF preference pairs.

---

## Engineering blog

Notes from building this — debugging stories, architecture posts, retrieval design. 17 posts across 5 series; a few representative ones below, full index at [remediatelabs.io/blog](https://remediatelabs.io/blog):

- **Agent Debugging** — [Why My AI Agent Kept Adding Null Checks Instead of Fixing the Bug](https://remediatelabs.io/blog/symptom-fix-antipattern) (producer/consumer routing) · [Why My AI Agent Cited a File That Never Existed](https://remediatelabs.io/blog/fabricated-file-citation) (fabrication under a code-reading requirement)
- **RAG Learnings** — [Why the Same Bug Kept Creating New Incidents](https://remediatelabs.io/blog/rag-dedup-failure) (four-failure-mode dedup bug) · [RAG Finds the Candidate. The Live Store Confirms the Truth.](https://remediatelabs.io/blog/rag-index-vs-live-store) (search index vs source of truth)
- **Code Graph in Production** — [We Built a Call Graph Because Our Agent Kept Breaking Callers It Never Knew About](https://remediatelabs.io/blog/code-graph-call-graph-reverse-index) · [Five Data Structures for a Call Graph](https://remediatelabs.io/blog/code-graph-data-structures)
- **Code RAG in Production** (10 parts) — [What Actually Gets Indexed](https://remediatelabs.io/blog/code-rag-what-gets-indexed) · [Hybrid Search — Closing the Vocabulary Gap](https://remediatelabs.io/blog/code-rag-hybrid-search) · [Cross-Encoder Re-Ranking — From Top-3 to Rank 1](https://remediatelabs.io/blog/code-rag-cross-encoder-reranking)
- **Agent Cost Engineering** — [Token Cost Engineering in Agent Loops](https://remediatelabs.io/blog/prompt-caching-react-loops) (prompt caching + state pruning)

---

## Deployment

ECS Fargate behind a Cloudflare-fronted ALB. CI/CD via GitHub Actions OIDC (no secrets in repo). Terraform module in [`infra/agent_platform/`](infra/agent_platform/). On `main` push, the build, push to ECR, and ECS deploy run automatically.

---

## Author

Built by [Siddharth Sukumar](https://github.com/sidreddy88) · [sidreddy88@gmail.com](mailto:sidreddy88@gmail.com) · [remediatelabs.io](https://remediatelabs.io)
