---
name: project-platform-state
description: Detailed summary of the agent-platform repo — architecture, agents, services, safety rails, and current state (2026-05-18)
metadata:
  type: project
---

# Agent Platform — Repo Summary (2026-05-18)

**Agent Platform** is an autonomous production incident remediation system. When an error fires in production, it automatically diagnoses the root cause, writes a code fix, tests it, opens a PR, and gets it reviewed — without human intervention unless the fix is high-risk.

---

## What it does

A CloudWatch alarm triggers a webhook. From there, the system runs a sequential 6-stage pipeline:

1. **Triage** — classifies the error as real/noise/duplicate, assigns P0–P3 severity
2. **Diagnosis** — fetches logs, searches the codebase, identifies root cause and the exact file/function to fix
3. **Fix generation** — writes the patch, runs tests in a Docker sandbox, retries on failure
4. **Code review** — self-critiques the fix, posts feedback on the PR
5. **Definition of Done gate** — 5 checks before marking ready: test passes, file grounded, no symptom-fix, blast radius safe
6. **Approval** — high-risk fixes require human approval before merge

End-to-end MTTR is ~6 minutes. Triage accuracy is 92% on a 100-case golden dataset.

---

## How it's built

**Backend:** FastAPI + Python, async throughout. Six AI agents all extend a shared `BaseAgent` that runs a ReAct loop (Thought → Action → Observation → Answer). Each agent has registered tools it can call during its loop.

**Models used:** Haiku for triage (cheap, high-volume), Sonnet for diagnosis and fix generation (complex reasoning), GPT-4.5 for code review via a config-driven LLM gateway.

**Key services:** ChromaDB RAG for semantic search over code and past incidents, GitHub API for PR operations, AWS SDK for CloudWatch logs and ECS health, SQLite/Postgres for incident state, Langfuse for full tracing of every LLM call.

**Frontend:** React + TypeScript dashboard with live WebSocket updates — incident feed, PR grid, service health pillars, pipeline metrics.

---

## Agents

| Agent | Model | Purpose |
|---|---|---|
| TriageAgent | Haiku | Classify real/noise/duplicate, assign P0–P3 |
| DiagnosisAgent | Sonnet | Fetch logs, search codebase, identify root cause, ground symbols |
| FixGenerationAgent | Sonnet | Write patch, sandbox test, commit, self-assess |
| CodeReviewAgent | GPT-4.5 | Review PR, post feedback, recommend APPROVE/REQUEST_CHANGES |
| MergeDecisionAgent | Sonnet | Decide merge vs re-fix |
| CICDAgent | Sonnet | Monitor GitHub Actions, diagnose build failures |
| DeploymentAgent | Sonnet | Monitor AWS ECS/EC2/CloudWatch health |
| PerformanceAgent | Sonnet | Detect p95 latency / error rate regressions |
| ErrorClarityAgent | Sonnet | Generate observability PRs when root cause is unclear |
| MonitorGenerationAgent | Sonnet | Auto-create CloudWatch alarms after fix merges |

---

## Safety rails

- **Blast radius guard** — blocks PRs touching migrations, auth, secrets, or >5 files / >500 lines
- **Symbol grounding** — every file and function in a diagnosis verified to exist in repo before fix gen starts
- **Sandbox testing** — patch tested in Docker before PR opened; 3 attempts with error context on failure
- **Symptom-fix detection** — rejects null guards and try/catch at crash sites; forces fix at root cause
- **Dedup** — three layers: exact SQL match, regression check, RAG similarity (0.80 threshold)
- **DoD gate** — 5 checks before REVIEWING transition
- **Approval gate** — HIGH/CRITICAL risk actions require human approval

---

## Key architectural decisions

- **SQLite over Postgres** — zero operational overhead beats replication safety at current scale
- **Haiku for triage** — 10x cheaper than Sonnet; sufficient for 3-way classification
- **Stack-trace-only file resolution** — historical bug: error type appeared in a comment; code search returned wrong file; bad fix nearly merged. Stack trace frames only.
- **PR_BASE = staging not main** — forking from main while targeting staging showed all intervening commits in the diff

---

## Incident state lifecycle (11 statuses)

```
OPEN → TRIAGING → NOISE / DUPLICATE (terminal)
                → DIAGNOSING → AWAITING_APPROVAL (low confidence)
                             → FIXING → AWAITING_FIX_APPROVAL
                                      → REVIEWING → VERIFICATION_FAILED (terminal)
                                                  → AWAITING_REFIX_APPROVAL
                                                  → AWAITING_APPROVAL → RESOLVED
                                                                       → REJECTED
```

---

## Current state (2026-05-18)

- Live at app.remediatelabs.io
- 621 passing tests, 9 pre-existing failures (mock drift, not logic bugs)
- No frontend tests — manual only
- Recently merged (PR #130): module-level JS patch support, grounding false positives fixed, triage duplicate hallucination fixed, FIX_FAILED dedup blocking fixed, grep_codebase tool added, 14-day scan button

## Known gaps

- Single IncidentLoop instance — processes sequentially; no parallelism within a severity tier
- In-memory EventQueue — not persisted across restarts
- RAG goes stale — no auto-reindex on PR merge
- 9 pre-existing test failures — mock drift from impl changes
- No frontend test suite
- Symptom-fix detection is regex-based — edge cases slip through
- Docker sandbox assumes npm/jest or pytest available; fails silently if not

---

## Interview story

Not a toy — runs against a real production codebase (TargetApp), has caught and fixed real bugs, measurable accuracy metrics (92% triage, <8% false-positive, ~6 min MTTR). Every layer is defensible: why Haiku vs Sonnet, why stack-trace-only, why SQLite, why the DoD gate exists.
