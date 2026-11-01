"""
Candidate models and prices for `agentstats monitor --models`, from config/llm_routing.json (the prices the gateway
bills with), so the weekly report's cost estimates can't drift from them. An optional "retirements" map there
({"model": "YYYY-MM-DD"}, from the provider's deprecation notices) makes the report warn before a model in use retires.

    python scripts/agentstats_models.py --out models.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROUTING = Path(__file__).resolve().parent.parent / "config" / "llm_routing.json"


def models(routing: dict) -> dict:
    """llm_routing's cost_per_1k_tokens as agentstats' prices_per_million."""
    out = {"prices_per_million": {m: {"input": round(p["input"] * 1000, 6), "output": round(p["output"] * 1000, 6)}
                                  for m, p in routing["cost_per_1k_tokens"].items()}}
    if routing.get("retirements"):           # {"model": "YYYY-MM-DD"}: the weekly report warns before these
        out["retirements"] = dict(routing["retirements"])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    Path(args.out).write_text(json.dumps(models(json.loads(ROUTING.read_text())), indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
