# Weekly agent monitoring with agentstats

`agentstats` runs agent-platform's monitoring: once a week (Mondays 14:00 UTC, or on demand from the Actions tab)
`.github/workflows/agentstats-weekly.yml` runs `scripts/agentstats_weekly.py`, which reads the past week's Langfuse
traces and opens one GitHub issue (label `agentstats-weekly`) with a section per agent. Every agent is a config file in
`config/agentstats/agents/`; TriageAgent is the full example (rules, adapter checks, rubric judge, review, promote).

| Step | What it does | agentstats |
|---|---|---|
| Fetch | every TriageAgent run, its tool calls and its LLM calls (Langfuse v2 observations API, joined by trace id) | |
| Rows | one row per run, tagged by source (production / local / eval) and model; errored runs are infrastructure failures | row format |
| Behaviour rules | `called_check_duplicate_pr`; `called_get_occurrence_count` when there's a log group; `count_matches_lookup` (the stated count is one the tool returned, or 1 when every lookup found none) | `summarize`, Wilson intervals |
| Judge | a sample of 40 runs, rubric `config/agentstats/triage_rubric.json`, 2 repeats; flagged unreliable above a 10% flip rate | `grade`, `judge` |
| Human review | 20 judged runs, blind sheet + key in the artifact | `review sample` / `review import` |
| New eval cases | production failures grouped by signature | `promote find` / `promote accept` |

**What the first run found (week ending 2026-10-30, 3,449 runs):** on Claude Haiku 4.5 (before the 2026-10-26 switch)
the count lookup was called in only 44% of production runs (CI 29% to 61%) and 29% of eval runs; on Haiku 5.5, 100% of
1,296 runs on every rule, at $0.0005 a run vs $0.0077. The judge independently flagged invented facts in 13 of 13
sampled Haiku 4.5 runs that skipped the lookup. 189 runs failed on the LLM circuit breaker or a low credit balance and
are reported as outages, not agent failures.

**Source.** Traces carry a Langfuse environment (`production` on ECS, `local` elsewhere, set in
`app/services/tracing.py`). Older traces say `default`; for those a synthetic log group means an eval run and a real
one means production, which then includes local replays of real incidents.

**Adding an agent** is a config file, `config/agentstats/agents/<name>.json`:

```json
{"agent": "FixGenerationAgent",
 "rules": [{"name": "tests_before_pr", "before": ["sandbox_tests", "create_pull_request"]}],
 "group_by": "error_type"}
```

Rules use agentstats' kinds on the run's recorded steps (`called`, `not_called`, `before`, `args`) or check the
agent's output (`{"output": "decision", "in": [...]}`, `{"output": "monitors", "min": 1}`); `"when"` limits a rule
to runs where it applies (`"called:<tool>"`, `"output:<field>>=<n>"`, `"input_json:<key>"`, or an adapter
condition). Logic a config can't express goes in a small adapter in `scripts/agentstats_adapters.py` (triage's
"stated count was looked up"). Add `"rubric"` for a judge. The agent has to be traced: `BaseAgent.run()` is, and
other entry points use `@trace_entry` with `record_step()` for the actions rules check.

Configured today: triage (full), diagnosis (full: `submitted_via_tool`, `read_before_first_submit`, adapter checks
`diagnosis_accepted` and `accepted_first_try`, a rubric, step-limit runs labelled `step_limit`), fix (`tests_before_pr`), ErrorClarity (`read_before_suggesting`), merge decision
(`valid_decision`), monitor generation (`monitors_when_files`), code review (`posted_when_asked`).

**Closing the loop** is in each issue: fill the review sheet and import it; approve candidates and add their events
(with your label) to the triage eval set.

Secrets: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `TOGETHER_API_KEY` (judge). Run locally:
`python scripts/agentstats_weekly.py --days 7 --out weekly/ [--judge]` with the same variables set.

**Diagnosis, measured before choosing its rules** (recorded DeepSeek runs, r9 + g500, 1,491 trials): runs that never
submitted a diagnosis passed 0% of the time (89% when they did), so `submitted_via_tool` is both monitored and gated;
reading a file before submitting made no difference (82% vs 83%), so `read_before_first_submit` is monitored only.
First data (14 days): the grounding gate rejected 26 of 71 submissions (37%), and 17 of 62 runs ran out of steps
without an accepted diagnosis.

**The diagnosis CI gate decides with agentstats** (`--tier full` in `app/evals/diagnosis_gate_policy.toml`), gate v2's
own paired test printing alongside for comparison. Simulated on main's baseline the two agree on 99.5-100% of PRs; on a
recorded full DeepSeek run both say PASS (+1.6 points).
