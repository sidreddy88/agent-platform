"""
Did anything diagnosis depends on change between two git refs? Used by the diagnosis gate (diagnosis-gate-v2.yml) to
decide whether a PR needs the gate, and whether the baseline is stale.

    python scripts/diagnosis_inputs_changed.py BASE HEAD     # exit 0 = changed (prints what), 1 = unchanged

Inputs: diagnosis.py, base.py (the shared ReAct loop), the diagnosis harness and its loader, and the parts of
config/llm_routing.json that diagnosis reads: its routing entry, the default models, and the prices of the models it
routes to. The rest of llm_routing.json (other agents' routing, other prices) isn't a diagnosis input: before this,
any edit to the file, e.g. moving triage to another model, ran the 434-case diagnosis gate (~$29) or failed it as stale.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

FILES = re.compile(r"^app/agents/((diagnosis|base)\.py$|harness/(diagnosis/|__init__\.py$))")
CONFIG = "config/llm_routing.json"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def _config(ref: str) -> dict:
    r = subprocess.run(["git", "show", f"{ref}:{CONFIG}"], capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 else {}


def diagnosis_config(cfg: dict) -> dict:
    """The parts of llm_routing.json that diagnosis reads."""
    entry = (cfg.get("routing") or {}).get("diagnosis") or {}
    models = {m for m in (entry.get("model"), entry.get("fallback_model")) if m}
    prices = cfg.get("cost_per_1k_tokens") or {}
    return {"routing.diagnosis": entry, "defaults": cfg.get("defaults"),
            "prices": {m: prices.get(m) for m in sorted(models)}}


def changed(base: str, head: str) -> list[str]:
    files = _git("diff", "--name-only", base, head).split()
    out = [f for f in files if FILES.search(f)]
    if CONFIG in files and diagnosis_config(_config(base)) != diagnosis_config(_config(head)):
        out.append(f"{CONFIG} (the diagnosis entry, defaults or diagnosis model prices)")
    return out


def main() -> int:
    base, head = sys.argv[1], sys.argv[2]
    found = changed(base, head)
    for f in found:
        print(f)
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())
