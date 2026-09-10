"""
Submit the formatted TriageAgent dataset to Together AI's fine-tuning
API. Pivoted here after OpenAI's self-serve fine-tuning API rejected the
job with a 403 (OpenAI has restricted new fine-tuning jobs to orgs with
prior fine-tuning history -- confirmed by hand this session, not
assumed) and after an initial mix-up with Fireworks AI (wrong provider
for the credential actually in hand -- confirmed via a 401 against
Fireworks' API and the user correcting which platform the key belongs
to). Together AI is a real, live self-serve fine-tuning platform for
open-weight models.

Dataset format is unchanged from the OpenAI/Fireworks attempts --
Together uses the identical {"messages": [...]} chat JSONL shape, so
app/evals/triage_train_openai.jsonl / triage_heldout_openai.jsonl are
directly reusable, no reformatting needed.

Base model: Qwen/Qwen3.5-9B (LoRA fine-tuning). Chosen over
meta-llama/Meta-Llama-3.1-8B-Instruct-Reference (also confirmed
fine-tunable via client.fine_tuning.model_limits) because Qwen3.5-9B has
real serverless *inference* pricing on Together ($0.17/$0.25 per 1M
input/output tokens) -- the whole point of this exercise is a
cost/latency comparison against the current prompted-Haiku pipeline
(Haiku 4.5: $1/$5 per 1M), and Llama-3.1-8B-Instruct-Reference is a
fine-tuning-only base with no serverless inference tier to compare
against. Both confirmed genuinely tunable by calling
client.fine_tuning.model_limits(model_name=...) directly, not assumed
from a name.

Real cost, real external job -- confirm before invoking --submit. Price
is estimated via client.fine_tuning.estimate_price(...) and printed
before job creation either way.

Usage:
    python scripts/finetune_triage_together.py --list-tunable-models
    python scripts/finetune_triage_together.py --submit
    python scripts/finetune_triage_together.py --status <job_id>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_TRAIN_PATH = Path(__file__).resolve().parent.parent / "app" / "evals" / "triage_train_openai.jsonl"
_HELDOUT_PATH = Path(__file__).resolve().parent.parent / "app" / "evals" / "triage_heldout_openai.jsonl"
BASE_MODEL = "Qwen/Qwen3.5-9B"
CANDIDATE_MODELS = [
    "Qwen/Qwen3.5-9B",
    "meta-llama/Meta-Llama-3.1-8B-Instruct-Reference",
]


def _load_creds() -> str:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    for line in env_path.read_text().splitlines():
        if line.startswith("TOGETHER_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("TOGETHER_API_KEY not found in .env")


def _client():
    from together import Together
    return Together(api_key=_load_creds())


def list_tunable_models() -> None:
    client = _client()
    print("Checking candidate base models via client.fine_tuning.model_limits(...):\n")
    for model_name in CANDIDATE_MODELS:
        try:
            limits = client.fine_tuning.model_limits(model_name=model_name)
            print(f"  {model_name}")
            print(f"    supports_full_training={limits.supports_full_training}  "
                  f"max_num_epochs={limits.max_num_epochs}  "
                  f"max_seq_length_sft={limits.max_seq_length_sft}")
        except Exception as e:
            print(f"  {model_name} -> NOT tunable ({str(e)[:150]})")


def submit(n_epochs: int) -> None:
    client = _client()

    print(f"Uploading {_TRAIN_PATH.name} ({_TRAIN_PATH.stat().st_size} bytes)...")
    train_file = client.files.upload(file=_TRAIN_PATH, purpose="fine-tune")
    print(f"  train file id: {train_file.id}")

    print(f"Uploading {_HELDOUT_PATH.name} as validation set...")
    val_file = client.files.upload(file=_HELDOUT_PATH, purpose="fine-tune")
    print(f"  validation file id: {val_file.id}")

    print(f"\nEstimating price (base model: {BASE_MODEL}, n_epochs={n_epochs})...")
    estimate = client.fine_tuning.estimate_price(
        training_file=train_file.id,
        validation_file=val_file.id,
        model=BASE_MODEL,
        n_epochs=n_epochs,
    )
    print(f"  {estimate}")

    print(f"\nCreating fine-tuning job (base model: {BASE_MODEL}, lora=True)...")
    job = client.fine_tuning.create(
        training_file=train_file.id,
        validation_file=val_file.id,
        model=BASE_MODEL,
        n_epochs=n_epochs,
        lora=True,
        suffix="triage-agent",
    )
    print(f"  job id: {job.id}")
    print(f"  status: {job.status}")
    print(f"\nPoll with: python scripts/finetune_triage_together.py --status {job.id}")


def check_status(job_id: str) -> None:
    client = _client()
    job = client.fine_tuning.retrieve(job_id)
    print(f"status: {job.status}")
    print(f"base model: {job.model}")
    # model_output_name is the real callable inference model id (e.g. via
    # client.chat.completions.create(model=...)) -- job.output_name doesn't
    # exist on this SDK version, discovered by inspecting the full object.
    print(f"fine-tuned model id: {getattr(job, 'model_output_name', None)}")
    print(f"token_count: {getattr(job, 'token_count', None)}")
    # total_price/train_price/eval_price are in nano-dollars (1e-9 USD).
    total_price = getattr(job, 'total_price', None)
    if total_price is not None:
        print(f"total_price: ${total_price / 1e9:.2f}")
    print(f"progress: steps_completed={getattr(job, 'steps_completed', None)} "
          f"epochs_completed={getattr(job, 'epochs_completed', None)} "
          f"total_steps={getattr(job, 'total_steps', None)}")

    events = client.fine_tuning.list_events(job_id)
    print("\nRecent events:")
    for e in events.data[-10:]:
        print(f"  [{e.created_at}] {e.message}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-tunable-models", action="store_true")
    parser.add_argument("--submit", action="store_true", help="Upload files and create the job")
    parser.add_argument("--n-epochs", type=int, default=3)
    parser.add_argument("--status", metavar="JOB_ID", help="Check status of an existing job")
    args = parser.parse_args()

    if args.list_tunable_models:
        list_tunable_models()
    elif args.submit:
        submit(args.n_epochs)
    elif args.status:
        check_status(args.status)
    else:
        parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
