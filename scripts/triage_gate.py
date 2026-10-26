"""
TriageAgent regression gate, measured with agentstats (github.com/sidreddy88/agentstats).

Replaces the old fixed-threshold gate (scripts/eval_triage_full_regression.py: fail if more than 6% of 80
cases miss). That gate compared against nothing, caught a doubling of the error rate (3% -> 6%) only ~35% of
the time, had to drop the 6 noisy error templates (32 cases) to get under its threshold, and failed on API
outages. This one:

  - uses all 114 held-out cases, noisy templates included: each case is compared with main's own measured
    rate on that case, so a case that flips 40% of the time on main is fine as long as it still flips ~40%;
  - compares the PR with a committed baseline measured on main (4 trials per case), using agentstats'
    fixed-cases paired test (the diagnosis gate v2 test plus a small-sample correction);
  - passes only when the change is shown to be no worse than main by more than the policy's margin
    (non-inferiority), so an under-powered run can't pass by accident;
  - keeps API failures (anthropic.APIError, an open circuit breaker) out of the score: they're infrastructure,
    counted and capped separately. Any other exception is the agent's fault and counts as a miss.

    python scripts/triage_gate.py run --trials 2 --out runs/triage-gate/pr.jsonl
    python scripts/triage_gate.py baseline --trials 4         # on main; commit the two files it writes
    agentstats gate --policy app/evals/triage_gate_policy.toml --tier pr \\
        --candidate runs/triage-gate/pr.jsonl --baseline app/evals/triage_gate_baseline.jsonl
    python scripts/triage_gate.py size --trials 2             # pass/inconclusive/fail rates by true change
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import math
import random
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CASES = ROOT / "app" / "evals" / "triage_heldout.jsonl"
BASELINE = ROOT / "app" / "evals" / "triage_gate_baseline.jsonl"
BASELINE_META = ROOT / "app" / "evals" / "triage_gate_baseline.json"
POLICY = ROOT / "app" / "evals" / "triage_gate_policy.toml"

# Everything the triage replay's outcome depends on. The baseline records a hash of these files' contents, so
# "is the baseline stale?" is a content question (robust to squash merges), not a commit-history one.
INPUTS = ["app/agents/triage.py", "app/agents/base.py", "app/services/llm.py", "app/agents/harness/__init__.py",
          "app/services/ipi_guard.py", "app/services/output_validator.py", "config/llm_routing.json",
          "app/evals/triage_heldout.jsonl", "scripts/eval_triage_regression.py"]


def inputs_hash(ref: str | None = None) -> str:
    """sha256 over the input files, from the working tree or from a git ref (`git show REF:path`)."""
    import hashlib

    h = hashlib.sha256()
    for path in INPUTS:
        if ref:
            r = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, cwd=ROOT)
            data = r.stdout if r.returncode == 0 else b"<absent>"
        else:
            f = ROOT / path
            data = f.read_bytes() if f.exists() else b"<absent>"
        h.update(path.encode() + b"\0" + data + b"\0")
    return h.hexdigest()[:16]


def _infra(exc: Exception) -> str | None:
    """API and circuit-breaker failures are infrastructure; anything else is the agent's (or the PR's) fault."""
    import anthropic

    from app.services.circuit_breaker import CircuitOpenError
    if isinstance(exc, (anthropic.APIError, CircuitOpenError)):
        return type(exc).__name__
    return None


_TOKENS: contextvars.ContextVar[dict | None] = contextvars.ContextVar("triage_gate_tokens", default=None)


def _count_tokens() -> None:
    """Add every LLM call's input/output tokens to the running attempt's counter (asyncio tasks copy the context)."""
    from app.services.llm import LLMService

    original = LLMService.complete
    if getattr(original, "_counts_tokens", False):
        return

    async def complete(self, *args, **kwargs):
        out = await original(self, *args, **kwargs)
        counter = _TOKENS.get()
        if counter is not None:
            counter["model"] = self._model
            counter["input"] += self.last_input_tokens or 0
            counter["output"] += self.last_output_tokens or 0
        return out

    complete._counts_tokens = True
    LLMService.complete = complete


def _cost(tokens: dict) -> float | None:
    """List-price cost from config/llm_routing.json (input and output; prompt-cache discounts not counted)."""
    prices = json.loads((ROOT / "config" / "llm_routing.json").read_text()).get("cost_per_1k_tokens", {})
    p = prices.get(tokens.get("model") or "")
    return round((tokens["input"] * p["input"] + tokens["output"] * p["output"]) / 1000, 6) if p else None


async def _attempt(case: dict, trial: int, arm: str, sem: asyncio.Semaphore) -> dict:
    from scripts.eval_triage_regression import _replay_one

    async with sem:
        started = time.monotonic()
        row = {"arm": arm, "run": trial, "case": case["id"], "tags": [f"template:{case['input'].get('error_type')}"]}
        tokens = {"model": None, "input": 0, "output": 0}
        _TOKENS.set(tokens)
        try:
            r = await _replay_one(case)
            row.update(passed=r["verdict"] == "PASS", infra_error=None, verdict=r["verdict"], detail=r["detail"])
            if r.get("no_answer"):      # no usable answer: a failure even if the fallback matched the label
                row.update(passed=False, no_answer=r["no_answer"])
        except Exception as exc:  # noqa: BLE001 - classify, never crash the run
            kind = _infra(exc)
            row.update(passed=None if kind else False, infra_error=kind, verdict="ERROR",
                       detail=f"{type(exc).__name__}: {exc}"[:300])
        row["latency_s"] = round(time.monotonic() - started, 2)
        if (cost := _cost(tokens)) is not None:
            row["cost_usd"] = cost
        return row


async def run(trials: int, out: Path, arm: str, concurrency: int, limit: int | None,
              model: str | None = None) -> list[dict]:
    cases = [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()][:limit]
    _count_tokens()
    if model:      # run TriageAgent on another model (e.g. to compare one before switching), same code otherwise
        import functools

        from app.agents import triage
        triage.TriageAgent.__init__ = functools.partialmethod(triage.TriageAgent.__init__, model=model)
    sem = asyncio.Semaphore(concurrency)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    with out.open("w") as f:
        for trial in range(1, trials + 1):
            results = await asyncio.gather(*(_attempt(c, trial, arm, sem) for c in cases))
            for r in results:
                f.write(json.dumps(r) + "\n")
            rows += results
            passed = sum(1 for r in results if r["passed"])
            infra = sum(1 for r in results if r["infra_error"])
            print(f"trial {trial}/{trials}: {passed}/{len(results)} pass, {infra} infra failures", flush=True)
    return rows


def size(trials: int, margin: float, sims: int, seed: int, rule: str = "drop", conf: float = 0.90) -> None:
    """How the gate behaves for a range of true changes, simulated from the baseline's per-case rates (each
    case's true rate drawn from its baseline trials with a Jeffreys prior, both sides simulated)."""
    from agentstats.comparisons import fixed_cases

    rows = [json.loads(line) for line in BASELINE.read_text().splitlines() if line.strip()]
    per_case: dict[str, list[bool]] = {}
    for r in rows:
        if r["passed"] is not None:
            per_case.setdefault(r["case"], []).append(bool(r["passed"]))
    rng = random.Random(seed)
    m = min(len(v) for v in per_case.values())
    print(f"{len(per_case)} cases, baseline {m} trials, PR {trials} trials, rule {rule}, margin {margin:.0%}, "
          f"{conf:.0%} interval")
    print("| true shift (log-odds) | mean drop | PASS | INCONCLUSIVE | FAIL |\n|---|---|---|---|---|")
    for shift in (0.0, 0.25, 0.5, 0.75, 1.0, 1.5):
        out = {"PASS": 0, "INCONCLUSIVE": 0, "FAIL": 0}
        drop = 0.0
        for _ in range(sims):
            a, b, d = {}, {}, 0.0
            for c, v in per_case.items():
                s = sum(v)
                p = min(max(rng.betavariate(s + 0.5, len(v) - s + 0.5), 1e-6), 1 - 1e-6)
                q = 1 / (1 + math.exp(-(math.log(p / (1 - p)) - shift)))
                d += p - q
                a[c] = (m, sum(rng.random() < p for _ in range(m)))
                b[c] = (trials, sum(rng.random() < q for _ in range(trials)))
            drop += d / len(per_case)
            iv = fixed_cases(a, b, conf)
            if rule == "drop":       # no_significant_drop: FAIL only when the whole interval is below -margin
                out["FAIL" if iv.high < -margin else "PASS"] += 1
            else:                    # no_regression (non-inferiority)
                out["PASS" if iv.low >= -margin else "FAIL" if iv.high < -margin else "INCONCLUSIVE"] += 1
        print(f"| {shift} | {100 * drop / sims:.1f} pts | {out['PASS'] / sims:.0%} | "
              f"{out['INCONCLUSIVE'] / sims:.0%} | {out['FAIL'] / sims:.0%} |")


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--trials", type=int, default=2)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--arm", default="candidate")
    r.add_argument("--concurrency", type=int, default=4)
    r.add_argument("--limit", type=int)
    r.add_argument("--model", help="run TriageAgent on this model instead of routing.triage.model")
    b = sub.add_parser("baseline")
    b.add_argument("--trials", type=int, default=4)
    b.add_argument("--concurrency", type=int, default=4)
    ih = sub.add_parser("inputs-hash", help="hash of the triage inputs, from the working tree or a git ref")
    ih.add_argument("--ref")
    s = sub.add_parser("size")
    s.add_argument("--trials", type=int, default=2)
    s.add_argument("--margin", type=float, default=0.05)
    s.add_argument("--sims", type=int, default=1000)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--rule", choices=["drop", "noninferiority"], default="drop")
    s.add_argument("--conf", type=float, default=0.90)
    args = ap.parse_args()

    if args.cmd == "run":
        asyncio.run(run(args.trials, args.out, args.arm, args.concurrency, args.limit, args.model))
    elif args.cmd == "baseline":
        asyncio.run(run(args.trials, BASELINE, "main", args.concurrency, None))
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
        routing = json.loads((ROOT / "config" / "llm_routing.json").read_text())
        model = json.dumps(routing).split('"triage"')[1].split('"model": "')[1].split('"')[0]
        BASELINE_META.write_text(json.dumps({
            "main_sha": sha, "inputs_hash": inputs_hash(), "trials": args.trials, "cases_file": str(CASES.relative_to(ROOT)),
            "model": model, "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }, indent=2) + "\n")
        print(f"wrote {BASELINE.relative_to(ROOT)} and {BASELINE_META.relative_to(ROOT)} (main {sha[:8]})")
    elif args.cmd == "inputs-hash":
        print(inputs_hash(args.ref))
    elif args.cmd == "size":
        size(args.trials, args.margin, args.sims, args.seed, args.rule, args.conf)
    return 0


if __name__ == "__main__":
    sys.exit(main())
