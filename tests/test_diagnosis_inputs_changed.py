"""scripts/diagnosis_inputs_changed.py: which llm_routing.json edits count as diagnosis changes."""
from scripts.diagnosis_inputs_changed import diagnosis_config

BASE = {"defaults": {"sonnet": "s", "haiku": "h"},
        "routing": {"diagnosis": {"model": "ds"}, "triage": {"model": "h45"}},
        "cost_per_1k_tokens": {"ds": {"input": 1}, "h45": {"input": 2}}}


def _with(**changes):
    import copy
    cfg = copy.deepcopy(BASE)
    for path, value in changes.items():
        node = cfg
        *keys, last = path.split("__")
        for k in keys:
            node = node.setdefault(k, {})
        node[last] = value
    return cfg


def test_other_agents_routing_and_prices_are_not_diagnosis_inputs():
    assert diagnosis_config(BASE) == diagnosis_config(_with(routing__triage={"model": "h55"}))
    assert diagnosis_config(BASE) == diagnosis_config(_with(cost_per_1k_tokens__h55={"input": 0.1}))


def test_diagnosis_routing_defaults_and_its_prices_are():
    assert diagnosis_config(BASE) != diagnosis_config(_with(routing__diagnosis={"model": "other"}))
    assert diagnosis_config(BASE) != diagnosis_config(_with(defaults__sonnet="s2"))
    assert diagnosis_config(BASE) != diagnosis_config(_with(cost_per_1k_tokens__ds={"input": 9}))
