# Fix-agent harness

Everything around the model that FixGenerationAgent's fix loop uses and the harness optimizer
may edit: the system prompt, the rule and workflow sections of the fix prompt, the no-edit nudge,
tool descriptions, loop settings, and `skills.prompt` (itemized lessons, empty by default).
Loaded with `load_harness("fix", harness_dir)`; `HARNESS_DIR_FIX` points a process at a candidate.
Production uses this directory unchanged; a snapshot test (tests/test_fix_harness.py) proves the
rendered prompts equal the ones that were hardcoded before.
