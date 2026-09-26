"""Code-checks trajectory grader."""
from __future__ import annotations

import json

from app.harness_optimizer.grader import grade, render, summarize


def step(i, name, inp, out):
    return {"iteration": i, "name": name, "input": json.dumps(inp) if isinstance(inp, dict) else inp,
            "output": out}


def test_flags_waste_loops_and_the_escalation_ending():
    rejection = "REJECTED — root_cause_snippet doesn't match lib/x.py"
    rec = {"instance_id": "case-1", "trial": 1, "verdict": "FAIL", "cost": {"cost_usd": 4.2},
           "llm_calls": [{}] * 15, "steps": [
               step(1, "search_codebase", {"query": "a"}, "RAG not configured — codebase search unavailable."),
               step(2, "search_codebase", {"query": "b"}, "RAG not configured — codebase search unavailable."),
               step(3, "get_file_contents", {"file_path": "lib/x.py"}, "x" * 12000),
               step(4, "get_file_contents", {"file_path": "lib/x.py"}, "x" * 12000),
               step(5, "grep_codebase", {"pattern": "q"}, "No matches for 'q' in *."),
               step(6, "submit_diagnosis", {"affected_file": "lib/x.py"}, rejection),
               step(7, "submit_diagnosis", {"affected_file": "lib/x.py"}, rejection),
               step(8, "submit_diagnosis", {"affected_file": "lib/x.py"}, rejection),
               step(9, "get_file_contents", {"file_path": "missing.py"}, "Could not fetch missing.py: 404"),
           ]}
    g = grade(rec)
    assert g.turns == 15 and g.cost_usd == 4.2
    assert g.unavailable_calls == {"search_codebase: search_codebase unavailable (RAG not configured)": 2}
    assert g.redundant_calls >= 1 and g.redundant_output_chars >= 12000
    assert g.empty_results == 1 and g.tool_errors == 1
    assert g.rejections == 3 and g.unproductive_rejection_rounds >= 1
    assert not g.accepted and g.submit_attempts == 3
    assert any("never reached an accepted submit_diagnosis" in f for f in g.flags)
    assert any("could not work here" in f for f in g.flags)


def test_accepted_run_checks_that_cited_files_were_read():
    rec = {"instance_id": "case-2", "verdict": "PASS", "cost": {"cost_usd": 0.8}, "steps": [
        step(1, "grep_codebase", {"pattern": "set_segments"}, "1 match(es):\nsrc/cb.py:654: x.set_segments("),
        step(2, "get_file_contents", {"file_path": "src/cb.py", "start_line": 630}, "code"),
        step(3, "submit_diagnosis", {"affected_file": "src/cb.py",
                                     "blast_radius": [{"file": "src/caller.py"}],
                                     "additional_fix_targets": [{"file": "src/other.py"}]},
             "Diagnosis accepted. Write a brief final Answer."),
    ]}
    g = grade(rec)
    assert g.accepted and g.cited_files == ["src/caller.py", "src/cb.py", "src/other.py"]
    assert g.ungrounded_citations == ["src/caller.py", "src/other.py"]
    assert not any("never reached" in f for f in g.flags)


def test_summary_aggregates_by_verdict_and_renders():
    recs = [{"instance_id": f"c{i}", "verdict": v, "cost": {"cost_usd": c}, "llm_calls": [{}] * t,
             "steps": [step(1, "search_codebase", {"query": "x"}, "RAG not configured.")]}
            for i, (v, c, t) in enumerate([("PASS", 1.0, 5), ("FAIL", 5.0, 15), ("FAIL", 3.0, 15)])]
    s = summarize([grade(r) for r in recs])
    assert s["by_verdict"]["FAIL"] == {"n": 2, "mean_turns": 15.0, "mean_cost_usd": 4.0}
    assert s["never_accepted"] == 3
    text = render(s)
    assert "Never reached an accepted submit_diagnosis: 3/3" in text
    assert "search_codebase unavailable" in text


def test_files_shown_by_symbol_verification_count_as_seen():
    """Same notion of 'retrieved' as DiagnosisAgent's output validator: on a
    real matplotlib-14623 run, ticker.py only ever appeared in a
    verify_symbol_in_repo result and was not flagged; axes/_base.py appeared
    in no tool result at all and was."""
    rec = {"instance_id": "c", "verdict": "PASS", "steps": [
        step(1, "verify_symbol_in_repo", {"symbol": "nonsingular"},
             "FOUND (2 match(es)) for 'nonsingular':\n  - lib/ticker.py  :: vmin, vmax = ..."),
        step(2, "submit_diagnosis", {"affected_file": "lib/ticker.py",
                                     "additional_fix_file": "lib/axes/_base.py"}, "Diagnosis accepted."),
    ]}
    assert grade(rec).ungrounded_citations == ["lib/axes/_base.py"]
