# Trace: IncidentResponseAgent — api service 0/3 tasks
**Date:** 2026-04-15 14:47:46 CDT  
**Trace ID:** b76b8ab9553dce69  
**Duration:** 2ms | **Iterations:** 2

---

## Issues Found

### 1. Agent skipped all diagnostic tools — hallucinated root cause
- Only 1 tool call in the entire trace: `request_action_approval`
- **Never called:** `gather_context`, `check_recent_deployments`, `search_logs`, `search_similar_incidents`, `generate_diagnosis`
- The LLM saw "api service 0/3 tasks" and immediately fabricated a root cause ("NPE crash-loop") and a specific version to roll back to ("v1.2.3") without gathering any real evidence
- **Root cause:** `REACT_SYSTEM` prompt says "When you have enough information to answer the user, output Answer" — the LLM interprets the alert text as sufficient to diagnose and shortcircuits all diagnostic steps

### 2. `generate_diagnosis` never called — no structured diagnosis stored
- The pipeline requires `generate_diagnosis` to produce a structured `DiagnosisResult` (confidence, evidence, affected_file, fix_approach)
- By jumping straight to `request_action_approval`, the incident state has no real diagnosis, no confidence score, no evidence list
- The `approval_service` request was created with a fabricated description

### 3. Duration 2ms — suspiciously fast
- 2 LLM calls + 1 tool call in 2ms is impossible with a real Anthropic API call (minimum ~500ms)
- Likely a demo/test run against a mocked LLM or replay — not a live production run
- If this is real, there may be a timing bug in the `trace_agent` decorator where `time.perf_counter()` is captured incorrectly

### 4. `service.name = "unknown_service"` in OpenTelemetry attributes
- All spans show `service.name: "unknown_service"` in resourceAttributes
- Should be set to `"agent-platform"` so spans are grouped correctly in Langfuse

---

## Fixes Needed

| # | Fix | File |
|---|-----|------|
| 1 | Add hard guard to `IncidentResponseAgent.run()` prompt: "You MUST call `gather_context` and `generate_diagnosis` before calling `request_action_approval` or outputting an Answer" | `app/agents/incident.py` |
| 2 | Same guard: "Do NOT call `request_action_approval` unless `generate_diagnosis` has already been called" | `app/agents/incident.py` |
| 3 | Investigate 2ms duration — verify `time.perf_counter()` placement in `trace_agent` wrapper is correct | `app/services/tracing.py` |
| 4 | Set `OTEL_SERVICE_NAME=agent-platform` in `.env` or configure via Langfuse SDK | `.env` / `app/services/tracing.py` |

---

## What a correct trace should look like

```
IncidentResponseAgent (agent span)
├── gather_context (tool)              ← iteration 1
├── check_recent_deployments (tool)    ← iteration 2
├── search_logs (tool)                 ← iteration 3
├── search_similar_incidents (tool)    ← iteration 4
├── generate_diagnosis (tool)          ← iteration 5
└── request_action_approval (tool)     ← iteration 6 (only if needed)
    └── Answer                         ← iteration 7
```
