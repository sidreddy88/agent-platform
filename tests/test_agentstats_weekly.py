"""scripts/agentstats_weekly.py: TriageAgent traces from Langfuse -> agentstats rows and the weekly report.
No network: Langfuse pages are faked."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "agentstats_weekly", Path(__file__).resolve().parent.parent / "scripts" / "agentstats_weekly.py")
w = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(w)

REAL_LG = "/ecs/TaskTargetApp"
SYNTH_LG = "ecs/ContainerProcess/synthetic"


def agent(tid, answer, env="default", level=None, status=None):
    return {"traceId": tid, "name": "TriageAgent", "type": "AGENT", "environment": env, "level": level,
            "statusMessage": status, "latency": 2.5, "metadata": {"iterations": 3},
            "input": json.dumps({"user_input": "ERROR EVENT:\n  error_type  : APP_CRASHED\n  title       : x\n"}),
            "output": json.dumps({"answer": json.dumps(answer)})}


def tool(tid, name, inp, out, t):
    return {"traceId": tid, "name": name, "type": "TOOL", "input": json.dumps(inp), "output": out, "startTime": t}


def gen(tid, model, cost):
    return {"traceId": tid, "type": "GENERATION", "model": model, "totalCost": cost}


def trace_set():
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


def rows_by_case():
    return {r["case"]: r for r in (w.to_row(t) for t in trace_set())}


def test_join_attaches_tools_in_order_model_and_cost():
    t1 = next(t for t in trace_set() if t["trace_id"] == "t1")
    assert [c["name"] for c in t1["calls"]] == ["get_occurrence_count", "check_duplicate_pr"]
    assert t1["model"] == "claude-haiku-5-5" and t1["cost_usd"] == pytest.approx(0.0005)


def test_rules_catch_a_skipped_lookup_with_an_invented_count():
    r = rows_by_case()
    assert r["t1"]["checks"] == {"called_check_duplicate_pr": True, "called_get_occurrence_count": True,
                                 "count_matches_lookup": True}
    assert r["t2"]["checks"]["called_get_occurrence_count"] is False          # log group known, count never looked up
    assert "count_matches_lookup" not in r["t2"]["checks"]                     # nothing looked up to compare with
    assert "model:claude-haiku-4-5-20251001" in r["t2"]["tags"]


def test_count_of_one_when_every_lookup_found_none_is_supported():
    assert rows_by_case()["t3"]["checks"]["count_matches_lookup"] is True
    assert w.count_supported(1, [0, 0]) and not w.count_supported(1, [0, 3]) and not w.count_supported(47, [15])


def test_source_from_environment_or_log_group():
    r = rows_by_case()
    assert "source:production" in r["t1"]["tags"]          # old trace ("default"), real log group
    assert "source:eval" in r["t4"]["tags"]                # synthetic log group
    assert "source:production" in r["t6"]["tags"]          # tagged by the environment
    local = w.join_traces([agent("t9", {"decision": "real", "severity": "P2"}, env="local")], [], [])[0]
    assert w.source(local) == "local"


def test_an_errored_run_is_an_infrastructure_failure_not_a_rule_violation():
    r = rows_by_case()["t5"]
    assert r["infra_error"].startswith("Circuit breaker") and r["checks"] == {}


def test_unparseable_answer_is_no_answer():
    t = w.join_traces([{**agent("t7", {}), "output": json.dumps({"answer": "I could not decide"})}], [], [])[0]
    assert w.to_row(t)["no_answer"] == "parse_error"


def test_report_flags_proven_rule_failures_and_keeps_raw_text_out(tmp_path):
    rows = [w.to_row(t) for t in trace_set()]
    rows += [w.to_row(w.join_traces([agent(f"s{i}", {"decision": "real", "severity": "P2", "occurrences_24h": 3})],
                                    [tool(f"s{i}", "check_duplicate_pr", {}, "NO_DUPLICATE", "1")], [])[0])
             for i in range(30)]
    summary = w.summarize_rules(rows)
    report = w.render(*[__import__("datetime").datetime(2026, 11, 2)] * 2, rows, summary, "", "n/a", "", ["t2"])
    assert "## Needs attention" in report and "| source / model | runs |" in report
    assert "Circuit breaker" in report                                     # outages are reported, not hidden
    assert "APP_CRASHED" not in report.split("## Needs attention")[0]     # no raw event text in the header


def test_severity_guide_is_read_from_the_triage_prompt():
    guide = w.severity_guide()
    assert guide.count("\n") == 3 and guide.startswith("P0") and ">100/24h" in guide


def test_fetch_follows_the_cursor():
    pages = iter([{"data": [{"id": 1}], "meta": {"cursor": "c1"}}, {"data": [{"id": 2}], "meta": {"cursor": None}}])
    seen = []

    def get(params):
        seen.append(params.get("cursor"))
        return next(pages)
    import datetime as dt
    rows = w.fetch_observations("AGENT", dt.datetime(2026, 1, 1), dt.datetime(2026, 1, 8), name="TriageAgent", get=get)
    assert [r["id"] for r in rows] == [1, 2] and seen == [None, "c1"]
