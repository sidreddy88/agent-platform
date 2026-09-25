"""Sharding + aggregation logic of the DiagnosisAgent regression gate.

No replays run here -- these cover the pure pieces that decide which cases a
shard gets and whether a set of shard results passes, including the
fail-closed behaviour when a shard is missing or incomplete.
"""
import json

import pytest

from scripts import eval_diagnosis_full_regression as gate


def _write_shard(tmp_path, shard, of, verdicts, expected=None):
    results = [{"instance_id": f"s{shard}-{i}", "verdict": v, "detail": "", "suite": "swebench"}
               for i, v in enumerate(verdicts)]
    payload = {"shard": shard, "of": of,
               "expected": len(verdicts) if expected is None else expected,
               "results": results}
    (tmp_path / f"shard-{shard}.json").write_text(json.dumps(payload))


def test_shards_partition_every_case_exactly_once():
    items = list(range(62))
    shards = [gate._select_shard(items, k, 8) for k in range(1, 9)]
    assert sorted(x for s in shards for x in s) == items
    assert max(map(len, shards)) - min(map(len, shards)) <= 1


def test_offset_spreads_production_and_swebench_over_one_numbering():
    production, swebench = list(range(6)), list(range(56))
    counts = [len(gate._select_shard(production, k, 8))
              + len(gate._select_shard(swebench, k, 8, offset=6)) for k in range(1, 9)]
    assert sum(counts) == 62
    assert max(counts) - min(counts) <= 1


def test_aggregate_passes_within_threshold(tmp_path):
    _write_shard(tmp_path, 1, 2, ["PASS"] * 27 + ["FAIL"] * 2)
    _write_shard(tmp_path, 2, 2, ["PASS"] * 26 + ["FAIL"])
    assert gate._aggregate(tmp_path, expect_shards=2) == 0


def test_aggregate_fails_above_threshold(tmp_path):
    _write_shard(tmp_path, 1, 2, ["PASS"] * 24 + ["FAIL"] * 4)
    _write_shard(tmp_path, 2, 2, ["PASS"] * 25 + ["ERROR"] * 3)
    assert gate._aggregate(tmp_path, expect_shards=2) == 1


@pytest.mark.parametrize("setup", [
    lambda p: _write_shard(p, 1, 2, ["PASS"] * 28),                      # shard 2 missing
    lambda p: (_write_shard(p, 1, 2, ["PASS"] * 28),
               _write_shard(p, 2, 2, ["PASS"] * 10, expected=28)),       # shard 2 cut short
    lambda p: (_write_shard(p, 1, 2, ["PASS"] * 28),
               _write_shard(p, 2, 3, ["PASS"] * 28)),                    # mismatched shard count
])
def test_aggregate_fails_closed_on_partial_runs(tmp_path, setup):
    setup(tmp_path)
    assert gate._aggregate(tmp_path, expect_shards=2) == 1


def test_aggregate_fails_when_nothing_ran(tmp_path):
    _write_shard(tmp_path, 1, 1, [])
    assert gate._aggregate(tmp_path, expect_shards=1) == 1


class _ProviderError(Exception):
    def __init__(self, status_code, message=""):
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.parametrize("exc,kind", [
    (_ProviderError(400, "Your credit balance is too low to access the Anthropic API."), "billing"),
    (_ProviderError(401, "invalid x-api-key"), "auth"),
    (_ProviderError(429, "rate_limit_error"), "rate_limit"),
    (_ProviderError(529, "overloaded_error"), "provider_outage"),
    (_ProviderError(400, "messages: field required"), None),   # a real bad request is ours
    (ValueError("no affected_file"), None),
])
def test_provider_failures_are_classified(exc, kind):
    assert gate._provider_failure(exc) == kind


def test_any_infra_case_invalidates_the_run(tmp_path, capsys):
    _write_shard(tmp_path, 1, 2, ["PASS"] * 28)
    _write_shard(tmp_path, 2, 2, ["PASS"] * 27)
    payload = json.loads((tmp_path / "shard-2.json").read_text())
    payload["expected"] += 1
    payload["results"].append({"instance_id": "x", "verdict": "INFRA", "infra_kind": "billing",
                               "detail": "replay raised: credit balance", "suite": "swebench"})
    (tmp_path / "shard-2.json").write_text(json.dumps(payload))
    assert gate._aggregate(tmp_path, expect_shards=2) == 1
    out = capsys.readouterr().out
    assert "RUN INVALID" in out and "billing: 1" in out
    assert "Gate FAILS" not in out  # reported as invalid, never as a regression


def test_billing_failure_stops_the_shard(monkeypatch):
    import asyncio
    import sys
    import types

    calls = []

    async def replay(instance, github):
        calls.append(instance["instance_id"])
        raise _ProviderError(400, "Your credit balance is too low")

    monkeypatch.setitem(sys.modules, "app.services.github",
                        types.SimpleNamespace(GitHubService=lambda: None))
    import scripts.eval_swebench_diagnosis as swe
    monkeypatch.setattr(swe, "_replay_one", replay)

    instances = [{"instance_id": f"i{n}", "repo": "r"} for n in range(4)]
    results = asyncio.run(gate._run_swebench_suite(instances))
    assert calls == ["i0"]
    assert [r["verdict"] for r in results] == ["INFRA"] * 4
    assert {r["infra_kind"] for r in results} == {"billing"}


def _patch_swebench_replay(monkeypatch, verdicts_by_id):
    """Replay stub returning scripted verdicts per instance, one per call."""
    import sys
    import types

    calls = []

    async def replay(instance, github):
        calls.append(instance["instance_id"])
        verdict = verdicts_by_id[instance["instance_id"]].pop(0)
        if isinstance(verdict, Exception):
            raise verdict
        return {"instance_id": instance["instance_id"], "verdict": verdict, "detail": ""}

    monkeypatch.setitem(sys.modules, "app.services.github",
                        types.SimpleNamespace(GitHubService=lambda: None))
    import scripts.eval_swebench_diagnosis as swe
    monkeypatch.setattr(swe, "_replay_one", replay)
    return calls


def test_non_pass_is_retried_once_and_flaky_pass_counts(monkeypatch):
    import asyncio

    calls = _patch_swebench_replay(monkeypatch, {
        "steady": ["PASS"],
        "flaky": ["FAIL", "PASS"],
        "broken": ["FAIL", "FAIL"],
    })
    instances = [{"instance_id": i, "repo": "r"} for i in ("steady", "flaky", "broken")]
    results = {r["instance_id"]: r for r in asyncio.run(gate._run_swebench_suite(instances))}

    assert calls == ["steady", "flaky", "flaky", "broken", "broken"]
    assert results["steady"]["verdict"] == "PASS" and not results["steady"]["flaky"]
    assert results["flaky"]["verdict"] == "PASS" and results["flaky"]["flaky"]
    assert results["flaky"]["attempts"] == ["FAIL", "PASS"]
    assert results["broken"]["verdict"] == "FAIL" and not results["broken"]["flaky"]


def test_provider_failure_is_not_retried(monkeypatch):
    import asyncio

    calls = _patch_swebench_replay(monkeypatch, {
        "a": [_ProviderError(429, "rate_limit_error")],
        "b": ["PASS"],
    })
    instances = [{"instance_id": i, "repo": "r"} for i in ("a", "b")]
    results = asyncio.run(gate._run_swebench_suite(instances))
    assert calls == ["a", "b"]
    assert [r["verdict"] for r in results] == ["INFRA", "PASS"]


def test_billing_failure_on_retry_still_stops_the_shard(monkeypatch):
    import asyncio

    calls = _patch_swebench_replay(monkeypatch, {
        "a": ["FAIL", _ProviderError(400, "Your credit balance is too low")],
        "b": ["PASS"],
    })
    instances = [{"instance_id": i, "repo": "r"} for i in ("a", "b")]
    results = asyncio.run(gate._run_swebench_suite(instances))
    assert calls == ["a", "a"]
    assert [r["verdict"] for r in results] == ["INFRA", "INFRA"]
