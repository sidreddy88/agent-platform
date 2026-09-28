"""
Hand-label trajectories against the rubric, in a markdown sheet.

    # 1. write a sheet of N trajectories (half passing, half failing) to fill in
    python scripts/rubric_label_sheet.py make runs/harness/r2-20260925 --n 30 --out labels.md

    # 2. open labels.md, and after each "answer:" write yes / no / na
    # 3. parse it into labels.jsonl for scripts/rubric_agreement.py
    python scripts/rubric_label_sheet.py parse labels.md --out labels.jsonl

The sheet is plain markdown so it can be filled in any editor, with no
interactive prompt. Each trajectory's full turn-by-turn view is included, the
same text the LLM judge sees (app/harness_optimizer/rubric.render), so human
and model grade the same evidence.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.harness_optimizer.rubric import ANSWERS, QUESTIONS, render  # noqa: E402

_ID = re.compile(r"^## (\S+)\s*$")
_ANS = re.compile(r"^- \*\*(\w+)\*\*.*?answer:\s*(\w*)\s*$")


def trajectories(run_dir: Path) -> list[dict]:
    out = []
    for f in sorted((run_dir / "evals").glob("*/k*/*.json")):
        for rec in json.loads(f.read_text())["trajectories"]:
            rec = dict(rec)
            rec["_id"] = f"{f.parent.parent.name}/{rec['instance_id']}/t{rec.get('trial')}"
            out.append(rec)
    return out


def make(run_dir: Path, n: int, out: Path, seed: int = 7) -> int:
    recs = trajectories(run_dir)
    rng = random.Random(seed)
    fails = [r for r in recs if r.get("verdict") != "PASS"]
    passes = [r for r in recs if r.get("verdict") == "PASS"]
    rng.shuffle(fails)
    rng.shuffle(passes)
    k = min(len(fails), n // 2)
    chosen = fails[:k] + passes[:n - k]
    rng.shuffle(chosen)
    parts = ["# Rubric labelling sheet", "",
             f"After each `answer:` write one of {', '.join(ANSWERS)}. Judge the process, not "
             "whether the verdict was right. Questions:", ""]
    parts += [f"- **{q}**: {text}" for q, text in QUESTIONS.items()]
    for rec in chosen:
        parts += ["", "---", "", f"## {rec['_id']}", "", "```", render(rec), "```", ""]
        parts += [f"- **{q}** answer: " for q in QUESTIONS]
    out.write_text("\n".join(parts) + "\n")
    print(f"wrote {len(chosen)} trajectories ({k} failing) to {out}")
    return 0


def parse(sheet: Path, out: Path) -> int:
    labels: dict[str, dict[str, str]] = {}
    current = None
    for line in sheet.read_text().splitlines():
        m = _ID.match(line)
        if m:
            current = m.group(1)
            labels[current] = {}
            continue
        m = _ANS.match(line)
        if m and current and m.group(1) in QUESTIONS:
            ans = m.group(2).lower()
            if ans in ANSWERS:
                labels[current][m.group(1)] = ans
    complete = {k: v for k, v in labels.items() if len(v) == len(QUESTIONS)}
    with out.open("w") as f:
        for tid, ans in labels.items():
            f.write(json.dumps({"id": tid, "answers": ans}) + "\n")
    print(f"parsed {len(labels)} trajectories, {len(complete)} fully labelled -> {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    mk = sub.add_parser("make")
    mk.add_argument("run_dir", type=Path)
    mk.add_argument("--n", type=int, default=30)
    mk.add_argument("--out", type=Path, default=Path("labels.md"))
    ps = sub.add_parser("parse")
    ps.add_argument("sheet", type=Path)
    ps.add_argument("--out", type=Path, default=Path("labels.jsonl"))
    args = parser.parse_args()
    return make(args.run_dir, args.n, args.out) if args.cmd == "make" else parse(args.sheet, args.out)


if __name__ == "__main__":
    sys.exit(main())
