"""scripts/agentstats_weekly.py + agentstats_adapters.py: Langfuse traces -> agentstats rows -> the weekly report,
config-driven per agent. No network: Langfuse pages are faked."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))
_spec = importlib.util.spec_from_file_location("agentstats_weekly", _SCRIPTS / "agentstats_weekly.py")
w = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(w)
from agentstats_adapters import ADAPTERS  # noqa: E402

TRIAGE = ADAPTERS["triage"]
REAL_LG = "/ecs/TaskTargetApp"
SYNTH_LG = "ecs/ContainerProcess/synthetic"
CONFIGS = {c["key"]: c for c in w.load_configs()}


def agent(tid, answer=None, env="default", level=None, status=None, name="TriageAgent", output=None, inp=None):
    return {"traceId": tid, "name": name, "type": "AGENT", "environment": env, "level": level,
            "statusMessage": status, "latency": 2.5, "metadata": {"iterations": 3},
            "input": json.dumps(inp if inp is not None else
                                {"user_input": "ERROR EVENT:\n  error_type  : APP_CRASHED\n  title       : x\n"}),
            "output": json.dumps(output if output is not None else {"answer": json.dumps(answer)})}


def tool(tid, name, inp, out, t):
    return {"traceId": tid, "name": name, "type": "TOOL", "input": json.dumps(inp), "output": out, "startTime": t}


def gen(tid, model, cost):
    return {"traceId": tid, "type": "GENERATION", "model": model, "totalCost": cost}


def triage_traces():
    a = [agent("t1", {"decision": "real", "severity": "P2", "occurrences_24h": 15, "reasoning": "r"}),
         agent("t2", {"decision": "real", "severity": "P1", "occurrences_24h": 156, "reasoning": "r"}),
         agent("t3", {"decision": "real", "severity": "P0", "occurrences_24h": 1, "reasoning": "r"}),
         agent("t4", {"decision": "real", "severity": "P2", "occurrences_24h": 7, "reasoning": "r"}),
         agent("t5", None, level="ERROR", status="Circuit breaker 'anthropic_llm' is OPEN"),
         agent("t6", {"decision": "real", "severity": "P2", "occurrences_24h": 9, "reasoning": "r"}, env="production")]
    a[1]["input"] = json.dumps({"user_input": f"ERROR EVENT:\n  error_type  : CASTERROR\n  log_group   : {REAL_LG}\n"})
    t = [tool("t1", "get_occurrence_count", {"log_group": REAL_LG}, f"15 occurrences of 'X' in {REAL_LG}", "1"),
         tool("t1", "check_duplicate_pr", {}, "NO_DUPLICATE: none", "2"),
         tool("t2", "check_duplicate_pr", {}, "NO_DUPLICATE: none", "1"),          # skipped the count, said 156
         tool("t3", "get_occurrence_count", {"log_group": REAL_LG}, "0 occurrences of 'A'", "1"),
         tool("t3", "get_occurrence_count", {"log_group": REAL_LG}, "0 occurrences of 'B'", "2"),
         tool("t3", "check_duplicate_pr", {}, "NO_DUPLICATE: none", "3"),
         tool("t4", "get_occurrence_count", {"log_group": SYNTH_LG}, "7 occurrences of 'X'", "1"),
         tool("t4", "check_duplicate_pr", {}, "NO_DUPLICATE: none", "2"),
         tool("t6", "get_occurrence_count", {"log_group": REAL_LG}, "9 occurrences of 'X'", "1"),
         tool("t6", "check_duplicate_pr", {}, "NO_DUPLICATE: none", "2")]
    g = [gen("t1", "claude-haiku-5-5", 0.0002), gen("t1", "claude-haiku-5-5", 0.0003),
         gen("t2", "claude-haiku-4-5-20251001", 0.007), gen("t3", "claude-haiku-5-5", 0.0005)]
    return w.join_traces(a, t, g)


def triage_rows():
    return {r["case"]: r for r in (w.to_row(t, CONFIGS["triage"]) for t in triage_traces())}


# ---- configs -----------------------------------------------------------------------------------------------

def test_every_shipped_config_loads_and_names_its_trace():
    assert {"triage", "fix", "error_clarity", "merge_decision", "monitor_generation", "code_review"} <= set(CONFIGS)
    assert CONFIGS["triage"]["agent"] == "TriageAgent" and CONFIGS["fix"]["agent"] == "FixGenerationAgent"


def test_a_bad_config_is_refused(tmp_path):
    (tmp_path / "x.json").write_text(json.dumps({"agent": "X", "rules": [{"name": "r", "called": "a", "before": []}]}))
    with pytest.raises(ValueError, match="exactly one"):
        w.load_configs(config_dir=tmp_path)
    (tmp_path / "x.json").write_text(json.dumps({"agent": "X", "adapter": "nope"}))
    with pytest.raises(ValueError, match="unknown adapter"):
        w.load_configs(config_dir=tmp_path)


# ---- triage (adapter) --------------------------------------------------------------------------------------

def test_join_attaches_tools_in_order_model_and_cost():
    t1 = next(t for t in triage_traces() if t["trace_id"] == "t1")
    assert [c["name"] for c in t1["calls"]] == ["get_occurrence_count", "check_duplicate_pr"]
    assert t1["model"] == "claude-haiku-5-5" and t1["cost_usd"] == pytest.approx(0.0005)


def test_triage_rules_catch_a_skipped_lookup_with_an_invented_count():
    r = triage_rows()
    assert r["t1"]["checks"] == {"called_check_duplicate_pr": True, "called_get_occurrence_count": True,
                                 "count_matches_lookup": True}
    assert r["t2"]["checks"]["called_get_occurrence_count"] is False
    assert "count_matches_lookup" not in r["t2"]["checks"]
    assert "model:claude-haiku-4-5-20251001" in r["t2"]["tags"] and "agent:triage" in r["t2"]["tags"]


def test_count_of_one_when_every_lookup_found_none_is_supported():
    assert triage_rows()["t3"]["checks"]["count_matches_lookup"] is True
    assert TRIAGE.count_supported(1, [0, 0]) and not TRIAGE.count_supported(1, [0, 3])
    assert not TRIAGE.count_supported(47, [15])


def test_source_from_environment_or_log_group():
    r = triage_rows()
    assert "source:production" in r["t1"]["tags"]          # old trace ("default"), real log group
    assert "source:eval" in r["t4"]["tags"]                # synthetic log group
    assert "source:production" in r["t6"]["tags"]          # tagged by the environment
    local = w.join_traces([agent("t9", {"decision": "real", "severity": "P2"}, env="local")], [], [])[0]
    assert w.source(local, TRIAGE) == "local"


def test_an_errored_run_is_an_infrastructure_failure_not_a_rule_violation():
    r = triage_rows()["t5"]
    assert r["infra_error"].startswith("Circuit breaker") and r["checks"] == {}


def test_unparseable_triage_answer_is_no_answer():
    t = w.join_traces([agent("t7", output={"answer": "I could not decide"})], [], [])[0]
    assert w.to_row(t, CONFIGS["triage"])["no_answer"] == "parse_error"


def test_severity_guide_is_read_from_the_triage_prompt():
    guide = TRIAGE.severity_guide()
    assert guide.count("\n") == 3 and guide.startswith("P0") and ">100/24h" in guide


# ---- the other agents (config rules on trace_entry spans) ---------------------------------------------------

def test_fix_tests_must_run_before_a_pr():
    cfg = CONFIGS["fix"]
    out = {"pr_number": 7, "files_changed": ["a.js"]}
    good = w.join_traces([agent("f1", name="FixGenerationAgent", output=out, inp={"error_type": "NULL_PTR"})],
                         [tool("f1", "sandbox_tests", {"attempt": 1}, "{'passed': True}", "1"),
                          tool("f1", "create_pull_request", {}, "{'pr_number': 7}", "2")])[0]
    bad = w.join_traces([agent("f2", name="FixGenerationAgent", output=out)],
                        [tool("f2", "create_pull_request", {}, "{'pr_number': 8}", "1")])[0]
    assert w.to_row(good, cfg)["checks"] == {"tests_before_pr": True}
    assert "error_type:NULL_PTR" in w.to_row(good, cfg)["tags"]
    assert w.to_row(bad, cfg)["checks"] == {"tests_before_pr": False}


def test_error_clarity_reads_before_suggesting():
    cfg = CONFIGS["error_clarity"]
    t = w.join_traces([agent("e1", name="ErrorClarityAgent", output={"additions": 1})],
                      [tool("e1", "suggest_addition", {"file": "a.js"}, "ok", "1"),
                       tool("e1", "read_file", {"path": "a.js"}, "...", "2")])[0]
    assert w.to_row(t, cfg)["checks"] == {"read_before_suggesting": False}


def test_merge_decision_output_must_be_a_valid_decision():
    cfg = CONFIGS["merge_decision"]
    ok = w.join_traces([agent("m1", name="MergeDecisionAgent", output={"decision": "merge_now"})], [])[0]
    odd = w.join_traces([agent("m2", name="MergeDecisionAgent", output={"decision": "ship it"})], [])[0]
    assert w.to_row(ok, cfg)["checks"] == {"valid_decision": True}
    assert w.to_row(odd, cfg)["checks"] == {"valid_decision": False}


def test_monitor_rule_applies_only_when_files_were_analysed():
    cfg = CONFIGS["monitor_generation"]
    none = w.join_traces([agent("g1", name="MonitorGenerationAgent", output={"monitors": 0, "files_analyzed": 0})],
                         [])[0]
    miss = w.join_traces([agent("g2", name="MonitorGenerationAgent", output={"monitors": 0, "files_analyzed": 3})],
                         [])[0]
    assert w.to_row(none, cfg)["checks"] == {}
    assert w.to_row(miss, cfg)["checks"] == {"monitors_when_files": False}


def test_code_review_posts_only_when_asked():
    cfg = CONFIGS["code_review"]
    asked = {"user_input": json.dumps({"owner": "o", "repo": "r", "pr_number": 1, "post_to_github": True})}
    not_asked = {"user_input": json.dumps({"owner": "o", "repo": "r", "pr_number": 1})}
    out = {"answer": "review text", "iterations": 3}
    t_asked = w.join_traces([agent("c1", name="CodeReviewAgent", inp=asked, output=out)], [])[0]
    t_not = w.join_traces([agent("c2", name="CodeReviewAgent", inp=not_asked, output=out)], [])[0]
    assert w.to_row(t_asked, cfg)["checks"] == {"posted_when_asked": False}
    assert w.to_row(t_not, cfg)["checks"] == {}


# ---- report ------------------------------------------------------------------------------------------------

def test_report_has_a_section_per_agent_flags_proven_failures_and_keeps_raw_text_out(tmp_path):
    rows = [w.to_row(t, CONFIGS["triage"]) for t in triage_traces()]
    rows += [w.to_row(w.join_traces([agent(f"s{i}", {"decision": "real", "severity": "P2", "occurrences_24h": 3})],
                                    [tool(f"s{i}", "check_duplicate_pr", {}, "NO_DUPLICATE", "1")], [])[0],
                      CONFIGS["triage"]) for i in range(30)]
    section = w.render_agent(CONFIGS["triage"], rows, w.summarize_rules(rows), "", "n/a", "", ["t2"])
    empty = w.render_agent(CONFIGS["merge_decision"], [], {}, "", "", "", [])
    report = w.render(dt.datetime(2026, 11, 2), dt.datetime(2026, 11, 9), [section, empty])
    assert "## TriageAgent" in report and "## MergeDecisionAgent" in report and "No runs this week." in report
    assert "**Needs attention**" in report and "Circuit breaker" in report
    assert "APP_CRASHED" not in report


def test_fetch_follows_the_cursor():
    pages = iter([{"data": [{"id": 1}], "meta": {"cursor": "c1"}}, {"data": [{"id": 2}], "meta": {"cursor": None}}])
    seen = []

    def get(params):
        seen.append(params.get("cursor"))
        return next(pages)
    rows = w.fetch_observations("AGENT", dt.datetime(2026, 1, 1), dt.datetime(2026, 1, 8), name="TriageAgent", get=get)
    assert [r["id"] for r in rows] == [1, 2] and seen == [None, "c1"]


# ---- diagnosis (adapter) ----------------------------------------------------------------------------------

def diag_trace(tid, calls, output="Diagnosis submitted"):
    a = agent(tid, name="DiagnosisAgent", output={"answer": output, "iterations": 5},
              inp={"user_input": "INCIDENT:\n  error_type      : APP_CRASHED\n  title           : x\n"})
    return w.join_traces([a], [tool(tid, n, i, o, str(k)) for k, (n, i, o) in enumerate(calls)])[0]


def test_diagnosis_uses_the_accepted_submission_and_tracks_rejections():
    cfg = CONFIGS["diagnosis"]
    t = diag_trace("d1", [("submit_diagnosis", {"root_cause": "guess"}, "REJECTED — fix the following ..."),
                          ("get_file_contents", {"file_path": "a.js"}, "code"),
                          ("submit_diagnosis", {"root_cause": "real", "affected_file": "a.js"},
                           "Diagnosis accepted. Write a brief final Answer")])
    row = w.to_row(t, cfg)
    assert ADAPTERS["diagnosis"].parse_output(t)["root_cause"] == "real"
    assert row["checks"] == {"submitted_via_tool": True, "read_before_first_submit": False,
                             "diagnosis_accepted": True, "accepted_first_try": False}
    assert "no_answer" not in row and "error_type:APP_CRASHED" in row["tags"]


def test_diagnosis_that_ran_out_of_steps_is_a_step_limit_no_answer():
    t = diag_trace("d2", [("grep_codebase", {"pattern": "x"}, "...")] * 15,
                   output="I was unable to find an answer within the allowed number of steps.")
    row = w.to_row(t, CONFIGS["diagnosis"])
    assert row["no_answer"] == "step_limit" and row["checks"]["submitted_via_tool"] is False
    assert row["checks"]["read_before_first_submit"] is True       # 'before' holds vacuously with no submission


def test_diagnosis_judge_item_puts_the_affected_files_reads_first():
    t = diag_trace("d3", [("get_file_contents", {"file_path": "b.js"}, "other"),
                          ("get_file_contents", {"file_path": "a.js"}, "the bug"),
                          ("submit_diagnosis", {"root_cause": "r", "affected_file": "a.js", "root_cause_snippet": "s"},
                           "Diagnosis accepted.")])
    item = ADAPTERS["diagnosis"].judge_item(t)
    assert item["code_the_agent_read"].startswith('get_file_contents {"file_path": "a.js"}')
    assert '"root_cause_snippet": "s"' in item["diagnosis"]


def test_review_helper_numbers_runs_in_sheet_order_with_links_and_no_judge_verdict(tmp_path):
    import csv
    (tmp_path / "review").mkdir()
    with (tmp_path / "review" / "review.csv").open("w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["item", "case", "arm", "run", "link", "human_verdict", "notes"])
        wr.writeheader()
        wr.writerow({"item": "x|t2|1", "case": "t2", "arm": "x", "run": 1})
        wr.writerow({"item": "x|t1|1", "case": "t1", "arm": "x", "run": 1})
    t = {"agent": {"projectId": "p1"}}
    link = w.trace_link(t)
    assert link.endswith("/project/p1/traces/{case}")
    w.write_review_helper(tmp_path, {"t1": {"event": "E1"}, "t2": {"event": "E2"}}, link, ["event"])
    md = (tmp_path / "review" / "REVIEW.md").read_text()
    assert md.index("## 1. `t2`") < md.index("## 2. `t1`") and "/traces/t2" in md and "E2" in md
    assert "verdict" not in md.split("judge's verdict is deliberately not shown")[1]


def test_trajectories_for_suggest_skip_errored_runs_and_keep_tool_order():
    traces = triage_traces()
    traj = w.trajectories(CONFIGS["triage"], traces)
    assert len(traj) == len(traces) - 1                                     # t5 errored
    t1 = next(t for t in traj if t["case"] == "t1")
    assert t1["agent"] == "TriageAgent" and t1["passed"] is None
    assert t1["tools"] == ["get_occurrence_count", "check_duplicate_pr"] and '"severity": "P2"' in t1["end"]


def test_suggestions_section_runs_agentstats_suggest_against_the_configs(tmp_path, monkeypatch):
    calls = []

    def fake(*args):
        calls.append(args)
        return "TriageAgent  (40 runs)\n [1] MONITOR rule x\nwrote proposal.json. Accept items with ..."
    monkeypatch.setattr(w, "_agentstats", fake)
    assert w.suggestions(tmp_path) == "No trajectories this week."
    (tmp_path / "trajectories.jsonl").write_text('{"agent": "TriageAgent", "tools": []}\n')
    text = w.suggestions(tmp_path)
    assert calls[0][0] == "suggest" and str(w.CONFIG_DIR) in calls[0]
    assert "[1] MONITOR rule x" in text and "wrote proposal.json" not in text and "suggest-apply" in text
