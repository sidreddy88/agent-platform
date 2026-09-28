"""
Run the LLM rubric judge on hand-labelled trajectories and report agreement.

    python scripts/rubric_agreement.py runs/harness/r2-20260925 labels.jsonl
    python scripts/rubric_agreement.py runs/harness/r2-20260925 labels.jsonl --model claude-haiku-4-5

Judges exactly the trajectories in labels.jsonl (the ids written by
rubric_label_sheet.py), then reports per-question and overall agreement plus
Cohen's kappa. Real API spend: one small judge call per trajectory
(~$0.01 each on Haiku 4.5).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_MODEL = "claude-haiku-4-5"


async def run(run_dir: Path, labels_path: Path, model: str) -> dict:
    from app.harness_optimizer import rubric
    from app.services.llm import LLMService
    from scripts.rubric_label_sheet import trajectories

    human = {}
    for line in labels_path.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            if len(row["answers"]) == len(rubric.QUESTIONS):
                human[row["id"]] = row["answers"]
    recs = {r["_id"]: r for r in trajectories(run_dir) if r["_id"] in human}
    svc = LLMService(model=model)

    async def llm(system: str, prompt: str) -> str:
        return await svc.complete([{"role": "user", "content": prompt}], system=system)

    model_answers = {}
    for tid, rec in recs.items():
        model_answers[tid] = (await rubric.judge(rec, llm))["answers"]
    return {"model": model, "labelled": len(human), "judged": len(model_answers),
            **rubric.agreement(human, model_answers)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("labels", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(".env")
    print(json.dumps(asyncio.run(run(args.run_dir, args.labels, args.model)), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
