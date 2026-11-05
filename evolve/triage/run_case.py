"""
Run TriageAgent on ONE labelled case with a given harness directory, grade it, and print one JSON line: the
harness-evolve CommandEvaluator contract (the last line of stdout is what it reads).

    python evolve/triage/run_case.py HARNESS_DIR CASE_ID
    python evolve/triage/run_case.py --grade CASE_ID OUTPUT_FILE    grade a triage answer without running the
                                                                    agent (for the grader anchors)

The replay is the triage gate's (scripts/triage_gate.py): the case's recorded occurrence count and open-PR
answer are served by stubs, so the only network call is the model. A case passes when the decision and
severity match the label (or one of its accepted alternatives) and the agent gave a usable answer; the
fallback real/P2 after a parse failure or the step limit is a failure even when it matches.

Environment: TRIAGE_MODEL runs another model (default: config/llm_routing.json, as in production), e.g.
claude-haiku-4-5 to reproduce the skipped occurrence lookup.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

CASE_FILES = [ROOT / "app" / "evals" / "triage_train.jsonl", ROOT / "app" / "evals" / "triage_heldout.jsonl"]


def cases() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in CASE_FILES:
        for line in f.read_text().splitlines():
            if line.strip():
                c = json.loads(line)
                out[c["id"]] = c
    return out


def _label(case: dict) -> str:
    out = case["output"]
    alts = [f"{d}/{s}" for d, s in out.get("accept", []) if [d, s] != [out["decision"], out["severity"]]]
    return f"expected {out['decision']}/{out['severity']}" + (f" (also accepted: {', '.join(alts)})" if alts else "")


def grade_only(case_id: str, output_file: str) -> dict:
    """The grader alone, on a triage answer text: harness-evolve checks it against anchors.jsonl first."""
    from app.agents.triage import _parse_triage_result
    from scripts.eval_triage_regression import _no_answer, _score

    result = _parse_triage_result(Path(output_file).read_text())
    passed = _no_answer(result) is None and _score(cases()[case_id]["output"], result)["status"] == "PASS"
    return {"passed": passed, "cost_usd": 0.0}


def _cost(tokens: dict) -> float:
    """List price from config/llm_routing.json. A model priced under a dated id (claude-haiku-4-5-20251001) is
    found from its alias too. An unpriced model is an error: harness-evolve's budget needs real costs."""
    from scripts import triage_gate as gate

    if tokens["model"] is None:
        return 0.0
    if (cost := gate._cost(tokens)) is not None:
        return cost
    prices = json.loads((ROOT / "config" / "llm_routing.json").read_text()).get("cost_per_1k_tokens", {})
    for key in prices:
        if key.startswith(tokens["model"]) or tokens["model"].startswith(key):
            return gate._cost({**tokens, "model": key})
    raise SystemExit(f"no price for {tokens['model']!r} in config/llm_routing.json cost_per_1k_tokens")


def _provider_problem(exc: Exception) -> bool:
    """Billing, auth and rate limits stop the run (resumable) rather than scoring the case as a failure."""
    import anthropic

    return isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                            anthropic.RateLimitError)) or "credit balance" in str(exc).lower()


async def run(harness_dir: str, case_id: str) -> dict:
    os.environ["HARNESS_DIR_TRIAGE"] = str(Path(harness_dir).resolve())
    from scripts import triage_gate as gate
    from scripts.eval_triage_regression import _replay_one

    if model := os.environ.get("TRIAGE_MODEL"):
        import functools

        from app.agents import triage
        triage.TriageAgent.__init__ = functools.partialmethod(triage.TriageAgent.__init__, model=model)
    gate._count_tokens()
    gate._record_tools()
    case = cases()[case_id]
    tokens = {"model": None, "input": 0, "output": 0}
    calls: list[str] = []
    gate._TOKENS.set(tokens)
    gate._TOOLS.set(calls)
    try:
        r = await _replay_one(case)
    except Exception as exc:  # noqa: BLE001
        cost = _cost(tokens)
        if _provider_problem(exc):
            return {"passed": False, "cost_usd": cost, "provider_failure": f"{type(exc).__name__}: {exc}"[:300]}
        raise
    checks = gate._checks(case, calls)
    missed = [k.removeprefix("called_") for k, ok in checks.items() if not ok]
    detail = f"{r['verdict']}: {r['detail']}; answered {r['current_decision']}/{r['current_severity']}"
    if r.get("no_answer"):
        detail += f"; no usable answer ({r['no_answer']})"
    if missed:
        detail += f"; did not call {', '.join(missed)}"
    return {
        "passed": r["verdict"] == "PASS" and not r.get("no_answer"),
        "submitted": not r.get("no_answer"),
        "cost_usd": _cost(tokens),
        "detail": detail,
        "answer": _label(case),
        "steps": [{"name": name} for name in calls],
    }


if __name__ == "__main__":
    if sys.argv[1] == "--grade":
        print(json.dumps(grade_only(sys.argv[2], sys.argv[3])))
    else:
        print(json.dumps(asyncio.run(run(sys.argv[1], sys.argv[2]))))
