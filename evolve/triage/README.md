# harness-evolve for TriageAgent

Measures candidate versions of triage's harness (`app/agents/harness/triage/`: the prompt, the two tool
descriptions, the turn budget) and keeps a change only if it beats run-to-run noise. Step 2 of the production
improvement loop; accepted edits reach production only through a normal PR, the triage gate and a human merge.

| File | What it is |
|---|---|
| `run_case.py` | Runs TriageAgent on one case with a given harness directory and prints harness-evolve's JSON line: passed, cost, the tools it called, a detail line for the proposer, and the label (hidden from the proposer). `--grade` grades an answer text alone. |
| `config.py` | The agent profile (description, safety rules, task contract, setting bounds) and the case split. |
| `anchors.jsonl` | Answers with known verdicts; the run checks the grader against them before trusting it. |

The replay is the triage gate's: recorded occurrence counts and open-PR answers come from stubs, so the only
network call is the model. Pass = the decision and severity match the label (or an accepted alternative) and
the answer was usable. A skipped `get_occurrence_count` is reported in the detail line, not failed.

```bash
python evolve/triage/run_case.py app/agents/harness/triage synth_070bfd0a      # one case by hand
PROPOSER_MODEL=anthropic/claude-sonnet-5-5 harness-evolve check evolve/triage/config.py --budget 15 --rounds 3
PROPOSER_MODEL=anthropic/claude-sonnet-5-5 harness-evolve run evolve/triage/config.py --run-dir runs/triage-evolve-1 --budget 15 --rounds 3
```

`TRIAGE_MODEL=claude-haiku-4-5` runs the agent on Haiku 4.5, which skipped the occurrence lookup on real
incidents (the known-answer test for step 3).

Known limit: the evolve cases come from `triage_train.jsonl`, whose labels were built from Haiku 4.5's answers
and never got the 2026-10-25 review that added accepted alternatives to the held-out set. A change that matches
Haiku 4.5's habits can look like a gain on them; the held-out set (reviewed) is the check.
