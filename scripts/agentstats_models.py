"""
Candidate models and prices for `agentstats monitor --models`, from config/llm_routing.json (the prices the gateway
bills with), so the weekly report's cost estimates can't drift from them.

    python scripts/agentstats_models.py --out models.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROUTING = Path(__file__).resolve().parent.parent / "config" / "llm_routing.json"


def models(routing: dict) -> dict:
    """llm_routing's cost_per_1k_tokens as agentstats' prices_per_million."""
    return {"prices_per_million": {m: {"input": round(p["input"] * 1000, 6), "output": round(p["output"] * 1000, 6)}
                                   for m, p in routing["cost_per_1k_tokens"].items()}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    Path(args.out).write_text(json.dumps(models(json.loads(ROUTING.read_text())), indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
