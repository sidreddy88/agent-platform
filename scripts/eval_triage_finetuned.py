"""
Evaluate the Together AI fine-tuned Qwen3.5-9B triage model against the
108-case held-out set (app/evals/triage_heldout.jsonl), which was never
used in training. Ground truth is the real TriageAgent/Haiku output
already baked into each case's `output` field (see
scripts/generate_triage_synthetic_dataset.py / build_real_cloudwatch_cases.py).

Runs against a live, billed-per-minute dedicated endpoint (see
scripts/finetune_triage_together.py's docstring for why -- LoRA
fine-tunes require dedicated deployment on both Together and Fireworks,
no serverless per-token option exists for custom fine-tunes right now).
Endpoint id is passed in; this script does NOT create or delete the
endpoint itself -- teardown is the caller's responsibility (kept
separate deliberately: a crash in this script should never leave you
unsure whether the endpoint got torn down by a script you can't see).

Results are written to disk incrementally (app/evals/triage_finetuned_eval_results.jsonl)
so a crash partway through doesn't lose completed calls -- same
resilience discipline as the CloudWatch survey tool earlier this session.

Usage:
    python scripts/eval_triage_finetuned.py --model ep_CeqG4BEKXrLxEzJGez3Ff
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.finetune_triage_together import _client
from scripts.format_triage_for_finetuning import SYSTEM_PROMPT, _user_message

_HELDOUT_PATH = Path(__file__).resolve().parent.parent / "app" / "evals" / "triage_heldout.jsonl"
_RESULTS_PATH = Path(__file__).resolve().parent.parent / "app" / "evals" / "triage_finetuned_eval_results.jsonl"


def _call_model(client, model: str, case: dict) -> tuple[dict | None, float, str | None]:
    """Returns (parsed_json_or_None, latency_seconds, raw_content_or_error)."""
    t0 = time.monotonic()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_message(case)},
            ],
            max_tokens=250,
            temperature=0,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        elapsed = time.monotonic() - t0
        content = resp.choices[0].message.content
        if not content:
            return None, elapsed, f"empty content (finish_reason={resp.choices[0].finish_reason})"
        try:
            return json.loads(content), elapsed, content
        except json.JSONDecodeError:
            return None, elapsed, content
    except Exception as e:
        elapsed = time.monotonic() - t0
        return None, elapsed, f"ERROR: {e}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Endpoint id/name to call as the chat model")
    parser.add_argument("--limit", type=int, default=None, help="Only run the first N cases (debugging)")
    args = parser.parse_args()

    with _HELDOUT_PATH.open() as f:
        cases = [json.loads(line) for line in f]
    if args.limit:
        cases = cases[:args.limit]

    client = _client()
    results = []
    print(f"Running {len(cases)} held-out cases against {args.model}...\n")

    with _RESULTS_PATH.open("w") as out_f:
        for i, case in enumerate(cases):
            parsed, latency, raw = _call_model(client, args.model, case)
            expected = case["output"]
            decision_match = parsed is not None and parsed.get("decision") == expected["decision"]
            severity_match = parsed is not None and parsed.get("severity") == expected["severity"]
            exact_match = decision_match and severity_match

            record = {
                "id": case["id"],
                "expected": {"decision": expected["decision"], "severity": expected["severity"]},
                "predicted": parsed,
                "raw": None if parsed is not None else raw,
                "latency_s": latency,
                "decision_match": decision_match,
                "severity_match": severity_match,
                "exact_match": exact_match,
            }
            results.append(record)
            out_f.write(json.dumps(record) + "\n")
            out_f.flush()

            status = "OK " if exact_match else "MISS"
            print(f"[{i+1:>3}/{len(cases)}] {status}  {case['id']:<30} "
                  f"expected={expected['decision']}/{expected['severity']:<10} "
                  f"got={(parsed or {}).get('decision')}/{(parsed or {}).get('severity')}  "
                  f"{latency:.2f}s")

    n = len(results)
    n_parse_ok = sum(1 for r in results if r["predicted"] is not None)
    n_exact = sum(1 for r in results if r["exact_match"])
    n_decision = sum(1 for r in results if r["decision_match"])
    n_severity = sum(1 for r in results if r["severity_match"])
    latencies = [r["latency_s"] for r in results]

    print(f"\n{'='*60}")
    print(f"Total cases:        {n}")
    print(f"Parsed OK:          {n_parse_ok}/{n} ({n_parse_ok/n:.1%})")
    print(f"Exact match:        {n_exact}/{n} ({n_exact/n:.1%})")
    print(f"Decision match:     {n_decision}/{n} ({n_decision/n:.1%})")
    print(f"Severity match:     {n_severity}/{n} ({n_severity/n:.1%})")
    print(f"Latency mean:       {statistics.mean(latencies):.2f}s")
    print(f"Latency p50:        {statistics.median(latencies):.2f}s")
    print(f"Latency p95:        {sorted(latencies)[int(len(latencies)*0.95)]:.2f}s")
    print(f"\nWritten to {_RESULTS_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
