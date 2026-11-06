"""
Weekly self-improvement for TriageAgent (step 4 of the production loop). Run by .github/workflows/triage-evolve-weekly.yml.

    python scripts/triage_evolve_weekly.py decide RULES_JSON --out decision.json
    python scripts/triage_evolve_weekly.py apply RUN_DIR --decision decision.json --body-out pr.md

`decide` reads `agentstats monitor`'s triage/rules.json for last week's production traces and says whether to run
harness-evolve: only when production had enough runs and a behaviour rule or the usable-answer rate fell below its
floor. Few runs or nothing wrong means no run and no spend. Findings are counts only, never event text.

`apply` turns a finished harness-evolve run into a change: if it accepted an edit and the held-out comparison isn't
worse, it copies the evolved harness files into app/agents/harness/triage/, regenerates the pinned goldens and writes
the PR description (findings, the edit, the held-out result, cost). Otherwise it changes nothing. The PR is opened
as a draft for a human to review; the triage gate runs on it like on any other triage change.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "app" / "agents" / "harness" / "triage"

MIN_RUNS = 10          # below this, a week's rates say too little to act on
RULE_FLOOR = 0.95      # the triage gate's floor for its behaviour rules
MAX_NO_ANSWER = 0.05   # the triage gate's ceiling for runs with no usable answer


def decide(rules: dict) -> dict:
    """Whether last week's production triage gives a reason to run the optimizer."""
    prod = {src: r for src, r in rules.items() if src.startswith("production")}
    runs = sum(r.get("runs", 0) for r in prod.values())
    findings: list[str] = []
    for src, r in sorted(prod.items()):
        n = r.get("runs", 0)
        if n and r.get("no_answer", 0) / n > MAX_NO_ANSWER:
            findings.append(f"{src}: no usable answer in {r['no_answer']} of {n} runs")
        for check, est in sorted((r.get("checks") or {}).items()):
            if est.get("n") and est["estimate"] < RULE_FLOOR:
                findings.append(f"{src}: rule {check} held in {est['estimate']:.0%} of {est['n']} runs "
                                f"(95% CI {est['low']:.0%} to {est['high']:.0%}; floor {RULE_FLOOR:.0%})")
    if runs < MIN_RUNS:
        reason = f"only {runs} production triage runs last week (need {MIN_RUNS}); not enough to act on"
        return {"run": False, "runs": runs, "findings": findings, "reason": reason}
    if not findings:
        return {"run": False, "runs": runs, "findings": [], "reason": f"all rules held over {runs} production runs"}
    return {"run": True, "runs": runs, "findings": findings,
            "reason": f"{len(findings)} finding(s) over {runs} production runs"}


def _evolve_cli(*args: str) -> str:
    out = subprocess.run(["harness-evolve", *args], capture_output=True, text=True, cwd=ROOT)
    return (out.stdout or out.stderr).strip()


def apply(run_dir: Path, decision: dict) -> tuple[bool, str]:
    """(changed, PR body). Changes app/agents/harness/triage/ only when there's an accepted, non-worse edit."""
    state = json.loads((run_dir / "state.json").read_text())
    report_path = run_dir / "report" / "report.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    accepted = state.get("accepted") or []
    vs_single = (report.get("comparisons") or {}).get("vs_original_single_run") or {}
    spent = state.get("spent_usd", 0.0)
    if not accepted:
        return False, f"harness-evolve accepted no edit (${spent:.2f} spent). Nothing to propose."
    if not report:
        return False, f"harness-evolve stopped before its held-out check ({state.get('stop_reason')}); not proposing."
    if vs_single.get("direction") == "worse":
        return False, f"held-out check found the evolved harness worse ({report.get('verdict')}); not proposing."

    evolved = [f for f in (run_dir / "incumbent").iterdir() if f.name != "README.md"]
    if all((HARNESS / f.name).exists() and (HARNESS / f.name).read_bytes() == f.read_bytes() for f in evolved):
        return False, "the accepted edit is already on main (the harness files are identical); nothing to propose."
    for f in evolved:
        shutil.copy2(f, HARNESS / f.name)
    subprocess.run([sys.executable, "-m", "tests.triage_harness_scenarios"], check=True, cwd=ROOT)

    lines = [
        "## Weekly self-improvement: triage harness change (draft, needs review)",
        "",
        "Opened by `.github/workflows/triage-evolve-weekly.yml`. A person reviews and merges, or closes it; the "
        "triage gate runs on it like on any other triage change.",
        "",
        "### Why it ran",
        f"Last week's production monitoring: {decision['reason']}.",
        *[f"- {f}" for f in decision.get("findings", [])],
        "",
        "### The change",
        f"Accepted edits: {', '.join(accepted)} (each kept only because it beat run-to-run noise on the evolve cases).",
        "",
        "```",
        _evolve_cli("diff", str(run_dir))[:6000],
        "```",
        "",
        "### Held-out check (cases the optimizer never saw)",
        str(report.get("verdict", "")),
        "",
        (run_dir / "report" / "report.md").read_text()[:4000] if (run_dir / "report" / "report.md").exists() else "",
        "",
        f"Cost of this run: ${spent:.2f} (cap $10). Grading: label match and the gate's tool rules "
        "(`evolve/triage/run_case.py`). Evolve cases are labelled offline cases; production traces only decided "
        "whether to run.",
        "",
        "🤖 Generated with [Claude Code](https://claude.com/claude-code)",
    ]
    return True, "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decide")
    d.add_argument("rules", type=Path)
    d.add_argument("--out", type=Path, required=True)
    a = sub.add_parser("apply")
    a.add_argument("run_dir", type=Path)
    a.add_argument("--decision", type=Path, required=True)
    a.add_argument("--body-out", type=Path, required=True)
    args = ap.parse_args()
    if args.cmd == "decide":
        rules = json.loads(args.rules.read_text()) if args.rules.exists() else {}
        result = decide(rules)
        args.out.write_text(json.dumps(result, indent=1))
        print(("RUN: " if result["run"] else "SKIP: ") + result["reason"])
        for f in result["findings"]:
            print(f"  - {f}")
        return 0
    changed, body = apply(args.run_dir, json.loads(args.decision.read_text()))
    args.body_out.write_text(body)
    print(("CHANGED: " if changed else "NO CHANGE: ") + body.splitlines()[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
