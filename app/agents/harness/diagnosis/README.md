# DiagnosisAgent harness

Everything here is text the model sees, or a knob that shapes what it sees.
Code in `app/agents/diagnosis.py` decides *what* gets filled in and *when* each
template is used; these files decide the *wording and limits*. This directory is
the harness optimizer's edit surface. It evolves a copy, and an agent runs a
copy via `DiagnosisAgent(harness_dir=...)` or `HARNESS_DIR_DIAGNOSIS`.

| File | What it is | Placeholders |
|---|---|---|
| `task_prompt.prompt` | The first user message: incident fields, then the step-by-step procedure, output schema and confidence guide. ~17k chars, resent on every turn (cached since #260). | `error_type title description service log_group pattern task_id severity occurrences_24h blast_radius triage_reasoning log_context prior_knowledge stack_trace` |
| `log_context_fetched.prompt` | "Steps 1–3" block when CloudWatch logs were pre-fetched | `error_samples still_occurring occurrence_timeline` |
| `log_context_missing.prompt` | The same block when the incident has no log group (every SWE-bench case) | none |
| `prior_knowledge.prompt` | A matching past incident, framed as a lead to verify | `prior_context` |
| `stack_trace.prompt` | Files detected deterministically from the error text | `paths` |
| `tool_descriptions.json` | The description of each of the 10 tools, as shown in the system prompt | none |
| `settings.json` | `max_iterations`, `file_read_char_limit`, `grep_default_glob`, `grep_max_matches` | none |

Templates are rendered with `str.format`, so literal braces are written `{{ }}`.
They are `.prompt`, not `.md`: the Docker build's `.dockerignore` drops every
`*.md` (this README included), and `tests/test_diagnosis_harness.py` fails if a
harness file would be excluded from the image.

Not here yet, deliberately: the generic ReAct system prompt (`app/agents/base.py`,
shared by all 7 agents) and the grounding gate's rejection messages (still in
`diagnosis.py`). Both are candidates to move once the optimizer needs them.

`tests/test_diagnosis_harness.py` pins this directory to the pre-refactor
behaviour: the rendered prompts, tool descriptions and turn budget must match
`tests/fixtures/diagnosis_harness_golden/` byte for byte. An intentional edit to
the default harness means regenerating those goldens in the same commit.
