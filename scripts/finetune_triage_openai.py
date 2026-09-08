"""
Submit the formatted TriageAgent dataset to OpenAI's fine-tuning API.

Fine-tunes gpt-4o-mini to reproduce TriageAgent's real judgment (decision +
severity) from the same facts the real agent would have gathered via tool
calls (occurrence_count, has_existing_pr), given directly instead of
fetched -- see scripts/format_triage_for_finetuning.py's docstring for why.

Real cost, real external job: this uploads app/evals/triage_train_openai.jsonl
to OpenAI and creates a real fine-tuning job that costs real money and runs
on OpenAI's infrastructure (typically minutes to an hour+ for a dataset this
size). Not run automatically -- confirm before invoking.

Usage:
    python scripts/finetune_triage_openai.py --submit
    python scripts/finetune_triage_openai.py --status <job_id>
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TRAIN_PATH = Path(__file__).resolve().parent.parent / "app" / "evals" / "triage_train_openai.jsonl"
_HELDOUT_PATH = Path(__file__).resolve().parent.parent / "app" / "evals" / "triage_heldout_openai.jsonl"
BASE_MODEL = "gpt-4o-mini-2024-07-18"


def _load_creds() -> str:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    for line in env_path.read_text().splitlines():
        if line.startswith("OPENAI_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("OPENAI_API_KEY not found in .env")


def submit() -> None:
    import openai
    client = openai.OpenAI(api_key=_load_creds())

    print(f"Uploading {_TRAIN_PATH.name} ({_TRAIN_PATH.stat().st_size} bytes)...")
    with _TRAIN_PATH.open("rb") as f:
        train_file = client.files.create(file=f, purpose="fine-tune")
    print(f"  train file id: {train_file.id}")

    print(f"Uploading {_HELDOUT_PATH.name} as validation set...")
    with _HELDOUT_PATH.open("rb") as f:
        val_file = client.files.create(file=f, purpose="fine-tune")
    print(f"  validation file id: {val_file.id}")

    print(f"\nCreating fine-tuning job (base model: {BASE_MODEL})...")
    job = client.fine_tuning.jobs.create(
        training_file=train_file.id,
        validation_file=val_file.id,
        model=BASE_MODEL,
        suffix="triage-agent",
    )
    print(f"  job id: {job.id}")
    print(f"  status: {job.status}")
    print(f"\nPoll with: python scripts/finetune_triage_openai.py --status {job.id}")


def check_status(job_id: str) -> None:
    import openai
    client = openai.OpenAI(api_key=_load_creds())
    job = client.fine_tuning.jobs.retrieve(job_id)
    print(f"status: {job.status}")
    print(f"model: {job.fine_tuned_model}")
    print(f"trained_tokens: {job.trained_tokens}")
    if job.status == "failed":
        print(f"error: {job.error}")

    events = client.fine_tuning.jobs.list_events(fine_tuning_job_id=job_id, limit=10)
    print("\nRecent events:")
    for e in events.data:
        print(f"  [{e.created_at}] {e.message}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submit", action="store_true", help="Upload files and create the job")
    parser.add_argument("--status", metavar="JOB_ID", help="Check status of an existing job")
    args = parser.parse_args()

    if args.submit:
        submit()
    elif args.status:
        check_status(args.status)
    else:
        parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
