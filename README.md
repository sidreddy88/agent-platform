# agent-platform

> Autonomous incident remediation for production systems. CloudWatch alarm to fix-PR in single-digit minutes — no human in the loop until approval.

**Engineering blog:** [remediatelabs.io/blog](https://remediatelabs.io/blog) · **Architecture deep-dive:** [remediatelabs.io/projects/agent-platform](https://remediatelabs.io/projects/agent-platform)

---

## What it does

When a production error fires a CloudWatch alarm, the platform's pipeline:

1. **Detects** the alarm via SNS → HTTPS webhook (push-based; zero polling load on the production server).
2. **Triages** the event into real / noise / duplicate at P0–P3 severity using a Haiku-class classifier validated against a 575-case golden dataset (461 train / 114 held-out), CI-gated on a noise-floor threshold rather than a literal 100%-pass bar.
3. **Diagnoses** the root cause with DeepSeek-V4.1-Flash running an evolved harness (see [Self-improving harness](#self-improving-harness)); every named file, function and snippet is grounded against the actual repo before a diagnosis is accepted.
4. **Generates a fix** — writes a patch, runs it in a Docker sandbox against the real test suite, retries up to 3× on failure.
5. **Self-critiques** the fix, opens a GitHub PR, and queues the merge for human approval if HIGH/CRITICAL.

It is also a research substrate: every LLM call and tool execution is captured as a Langfuse span, every approval rejection feeds an RLHF preference dataset, and every resolved incident is auto-captured into the golden eval set. The diagnosis agent's harness is improved by an optimizer agent and every change to it is gated by a statistical regression test (below).

---

## Headline numbers

**Diagnosis agent, measured on SWE-bench Verified** (localization: naming the file the real fix changed):

| Metric | Value |
|---|---|
| Evolved harness vs original, 66 held-out cases from unseen repos | **78.8% → 89.9%** (+11.1pp, 95% CI +6.1 to +16.2); ties a best-of-2 rerun at ~60% of its cost |
| DeepSeek-V4.1-Flash, unchanged harness, all 500 cases | **87.0%** at $0.039 per diagnosis |
| DeepSeek + evolved harness vs Claude Sonnet 5 on the held-out cases | **89.9% vs 79.5%** at 4.7× lower cost |
| Diagnosis CI gate (434 cases, paired test) | catches a 3.5-point drop **85%** of the time (old gate: under 10%) at a **5%** false-alarm rate |

**Live pipeline** (from `GET /agents/pr-stats`, computed from every incident's actual PR outcome in the live deploy's Postgres `incidents` table, not reproducible from a fresh clone):

| Metric | Value | What it covers |
|---|---|---|
| Merged PRs | 11 | all opened by the agents: 6 fixes (FixGenerationAgent), 5 observability (ErrorClarityAgent) |
| Agent pipeline | ~6 min | detected event to open fix PR, averaged over the 6 fix PRs |
| Avg MTTR | 32 min | detected event to merged PR, including human review, over all 11 |

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
                                              │ DiagnosisAgent │  grounds every symbol against the repo (DeepSeek-V4.1-Flash)
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
                                              │                │  vs. observability-specific criteria (GPT-5.5)
                                              └────────┬───────┘
                                                        ▼
                                              ┌────────────────┐
                                              │ Approval gate  │  HIGH/CRITICAL → human · merge → MonitorGen
                                              └────────────────┘
```

Two independent detection paths feed the same dedup gate: a push-based SNS webhook (near-zero latency) and a `DetectionService` background loop polling CloudWatch Logs every 5 minutes as a backstop — this polls AWS's own CloudWatch API, not the target application's servers, so it adds no load there. Self-critique (Haiku) runs *inside* `FixGenerationAgent`, before the PR exists; `CodeReviewAgent` (GPT-5.5, via `litellm`) is a separate agent that reviews and comments *after* the PR is already open — two distinct steps, not one.

When `DiagnosisAgent`'s confidence lands below the fix threshold with no file identified, `ErrorClarityAgent` (`app/agents/error_clarity.py`) runs instead of `FixGenerationAgent` — it adds a logging or error-handling line so the *next* occurrence is diagnosable, and explicitly does not attempt the fix itself. That boundary is enforced structurally, not just by prompt: its only path to committing code requires a proposed change to add a net-new logging/error-handling call, or it's rejected outright and routed to a text-only recommendation instead — a config change or behavior fix, even a correct one, can't get through. `CodeReviewAgent` reviews PRs from both agents, with different criteria depending on which one opened it (root-cause/symptom-fix checks for a `FixGenerationAgent` PR; secrets/PII-leakage and behavior-change checks for an `ErrorClarityAgent` one, since there's no "root cause" to check against for a change that isn't a fix).

The pipeline runs on FastAPI with WebSocket streaming for the live dashboard. State lives in Postgres (`agent_runs`, `incidents`, `approvals`, `monitor_records`); RAG candidate matching lives in pgvector. The dashboard is a React + Vite bundle served from the same ECS container.

---

## Tech stack

| Layer | Choice |
|---|---|
| Agent runtime | Anthropic SDK · Claude Sonnet 4.6 (fix) / Haiku 4.5 (triage, critique) · DeepSeek-V4.1-Flash via Together (diagnosis) · GPT-5.5 (code review) · routed in `config/llm_routing.json` via litellm |
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
  agents/          # BaseAgent + the 7 production-pipeline agents (triage, diagnosis,
                   # fix, review, merge-decision, error-clarity, monitor-gen)
    harness/       # DiagnosisAgent's harness as files: prompts, tool descriptions, settings
  integrations/    # 5 optional target-integration agents (not in the core pipeline)
  harness_optimizer/ # the self-improving harness: proposer, critic, acceptance rules,
                   # evaluator, tripwires, checkpointed long-running loop
  evals/           # golden datasets, SWE-bench splits, gate v2 (paired regression test)
  api/routes/      # FastAPI route handlers
  core/config.py   # Settings via pydantic-settings
  models/          # Pydantic data models (ErrorEvent, IncidentState, etc.)
  services/        # LLM, GitHub, AWS, RAG, approvals, tracing, circuit_breaker, sandbox
mcp_server/        # MCP server exposing agents to Claude Desktop
infra/             # Terraform for ECS Fargate + Cloudflare + SNS
scripts/           # optimize_harness.py, gate_v2.py, eval_swebench_diagnosis.py, measure_mttr.py, …
targets/           # Fetched from S3 at container startup (scripts/fetch_target_harness.py) —
                   # empty on a fresh clone, not committed to this repo
tests/             # pytest test suite (1,187 tests, mocked — no live API calls)
docs/              # architecture notes
```

---

## Design decisions worth defending

**Push, not poll.** CloudWatch alarms → SNS → HTTPS webhook. Zero polling load on the production server. Detection latency drops from ~7d (human attention) to single-digit minutes.

**Hard blocks are deterministic, soft hints are LLM-shaped.** Dedup is a hard block (drop the event); regression context is a soft hint (prompt injection). Mixing the two created a four-failure-mode bug.

**Ground every symbol against the repo.** DiagnosisAgent verifies symbols against the local checkout first (definitions before references), a failed lookup says so (`VERIFY_ERROR`) instead of pretending the symbol doesn't exist, and a grounding gate rejects any diagnosis that doesn't quote code the agent actually read.

**Sandbox before PR.** Every fix runs in a Docker container against the real test suite. If tests fail, regenerate up to 3× before opening any GitHub noise.

**Scope enforced in code, not by prompt.** `ErrorClarityAgent`'s prompt already says "add visibility, don't fix the bug" — that alone didn't stop it from once suppressing a warning via a schema-option change with zero logging added. Its commit path now requires a net-new logging/error-handling call in the proposed diff; anything else is rejected regardless of how the model justifies it.

**Approval gate for HIGH/CRITICAL.** Configurable risk threshold; rejections are logged as RLHF preference pairs.

**LLM regression gates need a noise floor, not zero tolerance.** TriageAgent's CI gate initially required every one of ~80 held-out cases to match its recorded label exactly. Real repeated runs showed a different ~2-4% of cases flip on any given run — even at `temperature=0.0` — with no fixed "bad" subset to exclude down to zero. Fixed by gating on a 6% noise-floor threshold instead of a literal 100% bar.

---

## Self-improving harness

An optimizer agent (`app/harness_optimizer/`, `scripts/optimize_harness.py`) evolves DiagnosisAgent's harness (task prompt, tool descriptions, settings, control flow via settings) and keeps a change only if it can prove it:

- **GEPA-style proposals** ([paper](https://arxiv.org/abs/2507.19457)): an LLM reads failing traces and proposes one targeted edit per round.
- **RRSI-style regularization** ([paper](https://arxiv.org/abs/2609.24972), [reference implementation](https://github.com/google-research/rrsi), Apache-2.0; the acceptance rules and the critic prompt are adapted from it): a noise band calibrated from repeated trials of the unchanged harness, cost-aware acceptance, an LLM leakage critic plus a denylist of every benchmark case ID, repo and fix path, and an escalation guard.
- **Held out by repository:** xarray and sphinx (66 cases) are never shown to the optimizer; the final phase compares the original and evolved harness there, against a matched-budget rerun baseline ([Wang et al., AI2](https://arxiv.org/abs/2607.12227)).
- **Long-running:** checkpoints after every phase, a per-case result cache, a hard budget cap, tripwires that pause the run when its own measurements look broken, and a watchdog.

The first full run (Sonnet 5, 51 cases) rejected every edit as within noise, reproducing the finding that evolution rarely beats reruns. A power analysis showed the limit was sample size; rebuilt around 138 cases DeepSeek-V4.1-Flash actually fails, a 7.4-hour run (2,129 agent runs, surviving a provider outage) accepted two changes, a correction-loop prompt and retry on no submission, that raised held-out localization from 78.8% to 89.9%. The evolved prompt edit didn't transfer back to Sonnet (−2.3pp, not significant) while retry did (+8.3pp), so the harness shipped together with DeepSeek.

## Diagnosis regression gate

Every non-draft PR that changes diagnosis (the agent, the shared ReAct loop, the harness files, or the routed model) runs `diagnosis-gate-v2.yml`, a required check on `main`:

- replays all 434 SWE-bench Verified cases outside the held-out repos, 2 trials each, sharded 20 ways (~40 min, ~$29)
- compares **each case with main's own measured pass rate** (`app/evals/gate_v2_baseline.json`, 4 trials per case) in a paired test, with a threshold calibrated by bootstrap for a 5% false-alarm rate
- fails closed on a missing case or a stale baseline

Why paired: the old gate counted failures on 56 always-passing cases, which barely move under a mild regression. Modelled on the same baseline, it caught a 3.5-point drop under 10% of the time; this one catches it ~85% of the time. Validated with known-answer tests: an unchanged harness passes (z = +2.03), and cutting the turn budget from 15 to 8 fails (z = −24.2).

---

## Engineering blog

Notes from building this — self-improving harnesses, evals, retrieval design, cost engineering. A few representative posts below; full index at [remediatelabs.io/blog](https://remediatelabs.io/blog):

- **Self-Improving Harness** (7 parts) — [Designing a Self-Improving Agent Harness](https://remediatelabs.io/blog/designing-a-self-improving-agent-harness) · [Power Analysis, and the Run That Worked](https://remediatelabs.io/blog/power-analysis-and-the-run-that-worked) · [Running an Agent for 7 Hours](https://remediatelabs.io/blog/running-an-agent-for-7-hours)
- **Code Graph in Production** — [We Built a Call Graph Because Our Agent Kept Breaking Callers It Never Knew About](https://remediatelabs.io/blog/code-graph-call-graph-reverse-index) · [Five Data Structures for a Call Graph](https://remediatelabs.io/blog/code-graph-data-structures)
- **Code RAG in Production** (9 parts) — [What Actually Gets Indexed](https://remediatelabs.io/blog/code-rag-what-gets-indexed) · [Hybrid Search — Closing the Vocabulary Gap](https://remediatelabs.io/blog/code-rag-hybrid-search) · [Cross-Encoder Re-Ranking — From Top-3 to Rank 1](https://remediatelabs.io/blog/code-rag-cross-encoder-reranking)
- **Agent Cost Engineering** — [Token Cost Engineering in Agent Loops](https://remediatelabs.io/blog/prompt-caching-react-loops) (prompt caching + state pruning)

---

## Deployment

ECS Fargate behind a Cloudflare-fronted ALB. CI/CD via GitHub Actions OIDC (no secrets in repo). Terraform module in [`infra/agent_platform/`](infra/agent_platform/). On `main` push, the build, push to ECR, and ECS deploy run automatically.

---

## Credits

The self-improving harness and the evals build on published work:

- **RRSI** (Xia et al., [arXiv 2609.24972](https://arxiv.org/abs/2609.24972); [reference implementation](https://github.com/google-research/rrsi), Apache-2.0): the acceptance rules, the escalation guard, the edit history format, and the leakage critic, whose six reject classes and prompt wording are adapted from `rrsi/critic.py`. Reimplemented, not imported: `app/harness_optimizer/acceptance.py`, `critic.py`, `history.py`.
- **GEPA** (Agrawal et al., [arXiv 2507.19457](https://arxiv.org/abs/2507.19457)): reflective, trace-driven edit proposals (`app/harness_optimizer/proposer.py`).
- **Rethinking the Evaluation of Harness Evolution for Agents** (Wang et al., [arXiv 2607.12227](https://arxiv.org/abs/2607.12227)): the matched-budget rerun baseline in the held-out report (`app/harness_optimizer/report.py`).
- **SWE-bench** (Jimenez et al., [arXiv 2310.06770](https://arxiv.org/abs/2310.06770)) and its human-validated **SWE-bench Verified** subset ([dataset](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified)): the benchmark every diagnosis number above is measured on.
- **pass@k** unbiased estimator (Chen et al., [arXiv 2107.03374](https://arxiv.org/abs/2107.03374)) and **pass^k** for agent reliability (Yao et al., τ-bench, [arXiv 2406.12045](https://arxiv.org/abs/2406.12045)): `app/evals/pass_k.py`.

---

## Author

Built by [Siddharth Sukumar](https://github.com/sidreddy88) · [sidreddy88@gmail.com](mailto:sidreddy88@gmail.com) · [remediatelabs.io](https://remediatelabs.io)
