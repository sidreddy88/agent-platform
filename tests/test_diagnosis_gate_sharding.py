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
