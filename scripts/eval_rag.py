"""
RAG retrieval quality eval.

Run after seeding the incident corpus with real or synthetic incidents.

Usage:
    python scripts/eval_rag.py
    python scripts/eval_rag.py --threshold 0.75
    python scripts/eval_rag.py --verbose

Each test case defines a query, the incident_id that should appear in the top-3,
and optionally ids that should NOT appear (false-positive checks).

Output:
    ✓  query text                   top=0.912  rank=1
    ✗  query text                   top=0.743  (should_match not in top 3)
    ~  query text                   near-miss: should_match at rank=4 score=0.78

Exit code 0 if all pass, 1 if any fail.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import os

# Allow running from repo root without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# Test cases — edit these to match real incident IDs in your corpus
# ---------------------------------------------------------------------------

S3_ID       = "7b834ea9-34e3-40b3-93f0-3f2c6052db6a"  # canonical S3 NoSuchKey incident
SYNTAX_ID   = "c3c393e8-7187-4b21-a5f9-6b4e72f23c42"  # JSON parse SyntaxError
TYPEERR_ID  = "b90ed493-a21c-41fb-bb19-c95b9a11f15f"  # TypeError publish_decision

TEST_CASES: list[dict] = [
    # ── S3 NoSuchKey — exact wording ─────────────────────────────────────
    {
        "query": "S3 NoSuchKey error on missing file",
        "should_match": S3_ID,
        "should_not_match": [SYNTAX_ID, TYPEERR_ID],
        "notes": "Close to indexed text — should be easy recall",
    },
    # ── S3 NoSuchKey — paraphrased ───────────────────────────────────────
    {
        "query": "file not found when copying object in S3 bucket",
        "should_match": S3_ID,
        "should_not_match": [SYNTAX_ID, TYPEERR_ID],
        "notes": "Different wording, same concept — tests semantic recall",
    },
    # ── S3 NoSuchKey — root cause angle ──────────────────────────────────
    {
        "query": "S3 delete operation key no longer exists no existence check",
        "should_match": S3_ID,
        "should_not_match": [SYNTAX_ID, TYPEERR_ID],
        "notes": "Query matches root cause text, not error description",
    },
    # ── JSON SyntaxError — exact wording ─────────────────────────────────
    {
        "query": "SyntaxError unexpected token JSON parse failed openai response",
        "should_match": SYNTAX_ID,
        "should_not_match": [S3_ID],   # TYPEERR shares validationCheck tokens — ok in top-3
        "notes": "Close to indexed text",
    },
    # ── JSON SyntaxError — paraphrased ───────────────────────────────────
    {
        "query": "openai returning markdown fenced JSON instead of plain JSON classifyFields",
        "should_match": SYNTAX_ID,
        "should_not_match": [S3_ID],   # TYPEERR shares validationCheck tokens — ok in top-3
        "notes": "Root cause angle — response_format missing",
    },
    # ── TypeError — exact wording ────────────────────────────────────────
    {
        "query": "Cannot read properties of undefined reading publish_decision",
        "should_match": TYPEERR_ID,
        "should_not_match": [S3_ID],   # SYNTAX shares validationCheck tokens — ok in top-3
        "notes": "Exact error message fragment",
    },
    # ── TypeError — paraphrased ──────────────────────────────────────────
    {
        "query": "classification field missing on llm response validationCheck",
        "should_match": TYPEERR_ID,
        "should_not_match": [S3_ID],   # SYNTAX shares validationCheck tokens — ok in top-3
        "notes": "Root cause angle — incomplete object returned",
    },
    # ── Cross-type: should NOT confuse S3 with JSON error ────────────────
    {
        "query": "JSON parse error in validationOpenAI",
        "should_match": SYNTAX_ID,
        "should_not_match": [S3_ID],
        "notes": "Precision check — S3 incident must not outrank SYNTAX",
    },
    # ── Noise — should match nothing strongly ────────────────────────────
    {
        "query": "deployment pipeline green all tests passing no issues",
        "should_match": None,
        "should_not_match": [],
        "expect_top_below": 0.50,
        "notes": "Unrelated content — top score should be noise-level",
    },
]

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

RESET  = "\033[0m"
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
GREY   = "\033[90m"


async def run(threshold: float, verbose: bool) -> int:
    try:
        from app.services.rag import RAGService
        rag = RAGService()
    except Exception as exc:
        print(f"{RED}✗ RAG unavailable: {exc}{RESET}")
        return 1

    corpus_size = rag._incident_collection.count()
    if corpus_size == 0:
        print(f"{YELLOW}⚠ Incident corpus is empty — index some incidents first.{RESET}")
        print("  Trigger incidents via POST /incidents/scan and let them resolve.")
        return 1

    print(f"Corpus: {corpus_size} incident(s)   Threshold: {threshold}\n")

    passed = failed = skipped = 0

    for case in TEST_CASES:
        query = case["query"]
        should_match = case.get("should_match")
        should_not_match = case.get("should_not_match", [])
        expect_top_below = case.get("expect_top_below")
        notes = case.get("notes", "")

        results = await rag.search_incidents(query, n_results=10, min_score=0.0)
        top_score = results[0]["score"] if results else 0.0
        top_ids = [r["incident_id"] for r in results[:3]]
        all_ids = [r["incident_id"] for r in results]

        # ── Check: should_match in top 3 ────────────────────────────────
        if should_match is None:
            # No ground truth yet — just print what we got
            label = f"{GREY}?{RESET}"
            detail = f"top={top_score:.3f}  no ground truth set"
            skipped += 1
        elif should_match in top_ids:
            rank = top_ids.index(should_match) + 1
            score = next(r["score"] for r in results if r["incident_id"] == should_match)
            label = f"{GREEN}✓{RESET}"
            detail = f"top={top_score:.3f}  match at rank={rank} score={score:.3f}"
            passed += 1
        elif should_match in all_ids:
            rank = all_ids.index(should_match) + 1
            score = next(r["score"] for r in results if r["incident_id"] == should_match)
            label = f"{YELLOW}~{RESET}"
            detail = f"near-miss: match at rank={rank} score={score:.3f} (not in top 3)"
            failed += 1
        else:
            label = f"{RED}✗{RESET}"
            detail = f"top={top_score:.3f}  should_match not found in top 10"
            failed += 1

        # ── Check: should_not_match ──────────────────────────────────────
        false_positives = [i for i in should_not_match if i in top_ids]
        if false_positives:
            label = f"{RED}✗{RESET}"
            detail += f"  FALSE POSITIVE: {false_positives}"
            failed += 1

        # ── Check: expect_top_below ──────────────────────────────────────
        if expect_top_below is not None:
            if top_score >= expect_top_below:
                label = f"{RED}✗{RESET}"
                detail += f"  noise matched too strongly ({top_score:.3f} ≥ {expect_top_below})"
                failed += 1
            else:
                detail += f"  noise correctly low ({top_score:.3f} < {expect_top_below})"

        print(f"  {label}  {query:<50} {detail}")
        if notes and verbose:
            print(f"     {GREY}{notes}{RESET}")
        if verbose and results:
            for r in results[:3]:
                marker = "→" if r["incident_id"] == should_match else " "
                print(f"       {marker} [{r['score']:.3f}] {r['text'][:80]}")

    print(f"\n  {GREEN}{passed} passed{RESET}  {RED}{failed} failed{RESET}  {GREY}{skipped} skipped (no ground truth){RESET}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RAG retrieval quality eval")
    parser.add_argument("--threshold", type=float, default=0.80)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    # Load .env
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    exit_code = asyncio.run(run(args.threshold, args.verbose))
    sys.exit(exit_code)
