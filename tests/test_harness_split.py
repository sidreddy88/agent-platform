"""The committed evolve/held-out split is what the builder produces, and it
has the properties the design depends on."""
import json

from scripts import build_harness_split as b

SPLIT = json.loads(b.SPLIT.read_text())
HISTORY = json.loads(b.HISTORY.read_text())


def _all(split):
    e, h = split["evolve"], split["heldout"]
    return {
        "evolve": set(e["failing"]) | set(e["guards"]) | set(e["hard"]),
        "heldout": set(h["failing"]) | set(h["stable"]) | set(h["hard"]) | set(h["extended"]),
        "reserve": set(split["reserve"]),
        "excluded": set(split["excluded"]),
    }


SAMPLE = [json.loads(line) for line in b.SAMPLE.read_text().splitlines() if line.strip()]
EXTRA = [json.loads(line) for line in b.HELDOUT_EXTRA.read_text().splitlines() if line.strip()]


def test_committed_split_matches_the_builder():
    assert b.build(HISTORY, SAMPLE, EXTRA) == SPLIT


def test_sets_are_disjoint_and_cover_every_gate_case():
    s = _all(SPLIT)
    union = set().union(*s.values())
    assert sum(len(v) for v in s.values()) == len(union)
    assert union == {inst["instance_id"] for inst in SAMPLE + EXTRA}   # 100 sample + 45 held-out extras


def test_heldout_repos_never_appear_in_evolve():
    repo = {c: v["repo"] for c, v in SPLIT["case_stats"].items()}
    assert not {repo[c] for c in _all(SPLIT)["evolve"]} & set(SPLIT["heldout_repos"])
    assert {repo[c] for c in _all(SPLIT)["heldout"]} <= set(SPLIT["heldout_repos"])


def test_evolve_contains_every_evolve_repo_case_that_ever_failed():
    stats = SPLIT["case_stats"]
    failing_outside_heldout = {c for c, s in stats.items()
                               if s["fails"] > 0 and s["repo"] not in SPLIT["heldout_repos"]
                               and c not in SPLIT["excluded"]}
    assert failing_outside_heldout == set(SPLIT["evolve"]["failing"]) | set(SPLIT["evolve"]["hard"])


def test_hard_tier_is_exactly_the_sample_cases_outside_the_gate_set():
    gate = set(HISTORY["cases"])
    hard = set(SPLIT["evolve"]["hard"]) | set(SPLIT["heldout"]["hard"])
    assert hard == {i["instance_id"] for i in SAMPLE} - gate
    assert all(SPLIT["case_stats"][c]["source"] == "baseline_100_fail" for c in hard)


def test_heldout_has_failing_cases_to_improve_on():
    assert len(SPLIT["heldout"]["failing"]) >= 5


def test_guards_never_failed_and_come_from_distinct_repos():
    stats = SPLIT["case_stats"]
    guards = SPLIT["evolve"]["guards"]
    assert all(stats[g]["fails"] == 0 for g in guards)
    assert len({stats[g]["repo"] for g in guards}) == len(guards)


def test_billing_and_harness_hangs_are_not_counted_as_agent_failures():
    case = {"attempts": {"r": ["BILLING", "TIMEOUT", "INFRA", "PASS"]}}
    assert b._agent_attempts(case) == ["PASS"]


def test_extended_heldout_is_only_held_out_repos_and_never_evolve():
    ext = set(SPLIT["heldout"]["extended"])
    assert len(ext) == 45
    assert {SPLIT["case_stats"][c]["repo"] for c in ext} <= set(SPLIT["heldout_repos"])
    assert not ext & (set(SPLIT["evolve"]["failing"]) | set(SPLIT["evolve"]["hard"]) | set(SPLIT["evolve"]["guards"]))
