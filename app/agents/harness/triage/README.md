# TriageAgent harness

Everything here is text the model sees, or a knob that shapes what it sees. Code in `app/agents/triage.py`
decides *what* gets filled in and *when* each template is used; these files decide the *wording and limits*.
This directory is the harness optimizer's edit surface. It evolves a copy, and an agent runs a copy via
`TriageAgent(harness_dir=...)` or `HARNESS_DIR_TRIAGE`.

| File | What it is | Placeholders |
|---|---|---|
| `task_prompt.prompt` | The first user message: the event's fields, the steps, the severity and decision guides, the duplicate rule and the output JSON | `today id source error_type title description service log_group task_id detected_at description_prefix occurrence_instruction` |
| `occurrence_with_log_group.prompt` | Step 2 when the event has a log group: call `get_occurrence_count` | `log_group pattern` |
| `occurrence_no_log_group.prompt` | Step 2 when it has none | none |
| `tool_descriptions.json` | The description of each of the 2 tools, as shown in the system prompt | none |
| `settings.json` | `max_iterations` | none |

Templates are rendered with `str.format`, so literal braces are written `{{ }}`. They are `.prompt`, not
`.md`: the Docker build's `.dockerignore` drops every `*.md` (this README included).

Not here, deliberately: the model and temperature (`config/llm_routing.json` and `triage.py`), the generic ReAct
system prompt (`app/agents/base.py`, shared by all agents), and the parsing of the answer.

`tests/test_triage_harness.py` pins this directory: the rendered prompts, tool descriptions and turn budget
must match `tests/fixtures/triage_harness_golden/` byte for byte. Those goldens were captured from the code
before the prompt moved here, so they prove the move changed nothing. An intentional edit to the default
harness means regenerating them (`python -m tests.triage_harness_scenarios`) in the same commit.
