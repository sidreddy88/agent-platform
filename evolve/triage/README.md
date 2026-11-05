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
the answer was usable, and the agent called the tools the triage gate's rules require (`check_duplicate_pr`;
`get_occurrence_count` when there is a log group). The rules are part of the score because the training labels
can't see a skipped lookup: they were built from Haiku 4.5's answers, which skipped it and still match.

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

## Run 1 (2026-11-05): known-answer test on Haiku 4.5

Triage on Haiku 4.5 (`TRIAGE_MODEL=claude-haiku-4-5`), which skips the occurrence lookup; proposer and critic
Sonnet 5.5; 60 evolve cases x 2 trials, 3 rounds; 114 held-out cases x 2 trials. $6.79.

- Starting pass rate 0.583 (evolve), noise band 0.100.
- r1 (when-to-use note on `get_occurrence_count`) rejected, +3.3 pts. **r2 (step 2 of the prompt: call
  `get_occurrence_count` after `check_duplicate_pr`; `occurrences_24h` is its count and the severity thresholds
  apply to it) accepted, +41.7 pts, cost +21%.** r3 (duplicate-check description) rejected, +7.5 pts. The proposer
  found the skipped lookup from the evidence alone ("120 duplicate checks but only 23 occurrence-count calls").
- Held-out: pass@1 0.224 -> 0.614, +39.0 pts (95% CI +31.1 to +46.9); +28.9 pts (CI +20.2 to +37.7) vs the
  original's best of 2. 66 cases improved, 4 regressed.
- Decomposed: label accuracy 92.5% -> 93.0% (unchanged); occurrence lookup called 25.4% -> 64.9% (still under the
  gate's 95%). P0 labels 36/40 -> 32/40 (small n; forcing the lookup on Haiku 4.5 hurt outages before too); no
  usable answer 0.4% -> 1.3%.
