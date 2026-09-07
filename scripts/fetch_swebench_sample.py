"""
Fetch a small, diverse sample of SWE-bench Verified into a local JSONL file,
for scripts/eval_swebench_diagnosis.py to replay DiagnosisAgent against.

Real motivation: "does DiagnosisAgent's bug-localization approach generalize
beyond AllInterviews" needs a credible, third-party, non-self-graded dataset.
SWE-bench Verified (500 instances, OpenAI-curated/human-validated subset of
SWE-bench) is real GitHub issues + PRs from real open-source repos, scored
against the real merged fix -- structurally the same task DiagnosisAgent does
in production (find which file/function is broken), just on someone else's
codebase.

Fetched via HuggingFace's datasets-server REST API (no `datasets` package
dependency, just httpx which is already a project dependency) -- one-time
fetch, saved locally. scripts/eval_swebench_diagnosis.py never talks to
HuggingFace at run time.

Deliberately STRATIFIED, not a random/first-N sample: SWE-bench Verified is
46% django/django (231/500) across only 12 total repos. This session already
found and diagnosed the exact failure mode of an eval dataset dominated by 2
categories (see the golden_dataset.jsonl skew finding) -- picking up to
`--per-repo` instances from EVERY repo, not just the biggest ones, avoids
repeating that mistake here.

Output: app/evals/swebench_verified_sample.jsonl. Public benchmark data (no
privacy concern, unlike app/evals/pipeline_regression.jsonl's real incident
data) -- committed, not gitignored.

Usage:
    python scripts/fetch_swebench_sample.py
    python scripts/fetch_swebench_sample.py --per-repo 3 --out /tmp/sample.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_DEFAULT_OUT = Path(__file__).resolve().parent.parent / "app" / "evals" / "swebench_verified_sample.jsonl"
_DATASET = "princeton-nlp/SWE-bench_Verified"
_API = "https://datasets-server.huggingface.co/rows"
_PAGE_SIZE = 100


def _get_with_retry(params: dict[str, Any], attempts: int = 4) -> httpx.Response:
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            resp = httpx.get(_API, params=params, timeout=30)
            resp.raise_for_status()
            return resp
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            last_exc = exc
            wait = 2 ** i
            print(f"  ...request failed ({exc.__class__.__name__}), retrying in {wait}s ({i + 1}/{attempts})")
            time.sleep(wait)
    raise RuntimeError(f"Giving up after {attempts} attempts") from last_exc


def _fetch_all_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        resp = _get_with_retry({"dataset": _DATASET, "config": "default", "split": "test",
                                 "offset": offset, "length": _PAGE_SIZE})
        data = resp.json()
        batch = [r["row"] for r in data["rows"]]
        rows.extend(batch)
        offset += _PAGE_SIZE
        if offset >= data["num_rows_total"]:
            break
    return rows


def _stratified_sample(rows: list[dict[str, Any]], per_repo: int) -> list[dict[str, Any]]:
    by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_repo[row["repo"]].append(row)
    sample: list[dict[str, Any]] = []
    for repo in sorted(by_repo):
        sample.extend(by_repo[repo][:per_repo])
    return sample


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-repo", type=int, default=2,
                         help="Max instances to keep per repo (default 2 -- 12 repos -> ~24 instances)")
    parser.add_argument("--out", default=str(_DEFAULT_OUT))
    args = parser.parse_args()

    print(f"Fetching all rows from {_DATASET} (test split)...")
    rows = _fetch_all_rows()
    print(f"Fetched {len(rows)} total instances across "
          f"{len({r['repo'] for r in rows})} repos.")

    sample = _stratified_sample(rows, args.per_repo)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for row in sample:
            f.write(json.dumps(row) + "\n")

    by_repo_counts = defaultdict(int)
    for row in sample:
        by_repo_counts[row["repo"]] += 1
    print(f"\nWrote {len(sample)} instance(s) to {out_path}:")
    for repo, count in sorted(by_repo_counts.items()):
        print(f"  {count:2d}  {repo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
