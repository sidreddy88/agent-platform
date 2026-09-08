"""
Convert the stratified train/held-out TriageAgent datasets
(app/evals/triage_train.jsonl, triage_heldout.jsonl) into OpenAI's
fine-tuning chat format ({"messages": [...]}), for training a small,
cheap classifier (gpt-4o-mini) to reproduce TriageAgent's real judgment
at lower cost/latency than the current prompted-Haiku-plus-tool-calls
pipeline.

Architectural simplification, stated plainly: the real TriageAgent is
agentic -- it calls check_duplicate_pr / get_occurrence_count as tools
mid-conversation. A fine-tuned single-shot classifier can't call tools,
so this format gives it the occurrence_count and has_existing_pr
directly in the input instead of making it fetch them. That's the actual
question this whole exercise is testing: can a single-shot classifier,
handed the same facts the agent would have gathered, reproduce the
agent's judgment without the tool-calling step -- not "can it replace
tool-calling agents in general."

System prompt is a direct, deliberately faithful adaptation of
TriageAgent's own real prompt (app/agents/triage.py) -- same decision
guide, same severity guide, same hard constraint on "duplicate" -- minus
the tool-calling instructions, since there are no tools to call here.

Usage:
    python scripts/format_triage_for_finetuning.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_TRAIN_IN = Path(__file__).resolve().parent.parent / "app" / "evals" / "triage_train.jsonl"
_HELDOUT_IN = Path(__file__).resolve().parent.parent / "app" / "evals" / "triage_heldout.jsonl"
_TRAIN_OUT = Path(__file__).resolve().parent.parent / "app" / "evals" / "triage_train_openai.jsonl"
_HELDOUT_OUT = Path(__file__).resolve().parent.parent / "app" / "evals" / "triage_heldout_openai.jsonl"

# Faithful adaptation of the real prompt in app/agents/triage.py -- same
# decision/severity guides and the same hard constraint on "duplicate",
# minus the tool-calling steps (this model gets the facts directly).
SYSTEM_PROMPT = """You are a triage agent. Classify this production error event.

You will be given the error event details, including whether occurrence \
count and duplicate-PR status have already been determined.

SEVERITY GUIDE:
  P0 — service down / data loss / blocking all users
  P1 — degraded / urgent, affecting many users or high frequency (>100/24h)
  P2 — silent recurring failure, needs a fix but not urgent (<100/24h)
  P3 — rare or very low impact (<5/24h)

DECISION GUIDE:
  "real"      — happening in production, needs investigation
  "noise"     — transient / expected / false alarm — stand down
  "duplicate" — open PR already covers this — link and stand down

CRITICAL: You may ONLY output decision="duplicate" if has_existing_pr is true. \
If has_existing_pr is false, you MUST NOT output "duplicate" even if you \
believe a fix is in progress. Use "real" if the error is happening and no \
open PR exists.

Answer with ONLY a valid JSON object, no other text:
{"decision": "real", "severity": "P2", "reasoning": "one sentence explanation"}"""


def _user_message(case: dict[str, Any]) -> str:
    inp = case["input"]
    return (
        f"ERROR EVENT:\n"
        f"  error_type       : {inp['error_type']}\n"
        f"  title            : {inp['title']}\n"
        f"  description      : {inp['description']}\n"
        f"  service          : {inp['service']}\n"
        f"  source           : {inp.get('source', 'cloudwatch')}\n"
        f"  occurrence_count : {inp['occurrence_count']} (in the last 24h)\n"
        f"  has_existing_pr  : {inp['has_existing_pr']}"
    )


def _assistant_message(case: dict[str, Any]) -> str:
    out = case["output"]
    return json.dumps({
        "decision": out["decision"],
        "severity": out["severity"],
        "reasoning": out["reasoning"],
    })


def _convert_file(in_path: Path, out_path: Path) -> int:
    with in_path.open() as f:
        cases = [json.loads(line) for line in f]

    with out_path.open("w") as f:
        for case in cases:
            record = {
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _user_message(case)},
                    {"role": "assistant", "content": _assistant_message(case)},
                ]
            }
            f.write(json.dumps(record) + "\n")
    return len(cases)


def main() -> int:
    n_train = _convert_file(_TRAIN_IN, _TRAIN_OUT)
    n_heldout = _convert_file(_HELDOUT_IN, _HELDOUT_OUT)
    print(f"Converted {n_train} train cases -> {_TRAIN_OUT.name}")
    print(f"Converted {n_heldout} heldout cases -> {_HELDOUT_OUT.name}")
    print("\nNote: the heldout file is in the same chat format for convenience, "
          "but for EVALUATION (not fine-tuning) you'd typically strip the "
          "assistant message and compare the fine-tuned model's real output "
          "against it -- see the eval step, not this conversion step.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
