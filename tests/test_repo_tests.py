"""Test mode: picking, running and comparing a repo's existing tests around a fix
(app/services/repo_tests.py and FixGenerationAgent._existing_tests_retry)."""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from app.agents.harness import DEFAULT_ROOT, load_harness
from app.services import repo_tests as rt


# ── pure helpers ──────────────────────────────────────────────────────────

def test_framework_for():
    assert rt.framework_for("django/django") == "django"
    assert rt.framework_for("sympy/sympy") == "sympy"
    assert rt.framework_for("pydata/xarray") == "pytest"


@pytest.mark.parametrize("path,stem", [
    ("django/db/models/query.py", "query"),
    ("sklearn/linear_model/_logistic.py", "logistic"),
    ("sklearn/cluster/k_means_.py", "k_means"),
    ("astropy/io/fits/__init__.py", "fits"),
])
def test_module_stem(path, stem):
    assert rt.module_stem(path) == stem


@pytest.mark.parametrize("name,symbol", [
    ("QuerySet.bulk_create", "bulk_create"),
    ("Quantity.__array_ufunc__", "Quantity"),
    ("__init__", None),
    ("the function handling this error", None),
    ("f", None),
    (None, None),
])
def test_search_symbol(name, symbol):
    assert rt.search_symbol(name) == symbol


def test_select_prefers_module_name_then_symbol_hits():
    files = ["tests/queries/test_query.py", "tests/other/test_query.py",
             "tests/bulk_create/tests.py", "tests/queries/tests.py"]
    hits = {"tests/bulk_create/tests.py": 9, "tests/queries/tests.py": 2,
            "django/db/models/query.py": 40}           # not a test file: ignored
    got = rt.select_test_files("django/db/models/query.py", files, hits, max_files=3)
    assert got[0] in ("tests/queries/test_query.py", "tests/other/test_query.py")
    assert got[2] == "tests/bulk_create/tests.py"
    assert "django/db/models/query.py" not in got
    assert len(rt.select_test_files("x/y.py", files, hits, max_files=1)) == 1


def test_select_falls_back_to_package_test_file():
    files = ["test_requests.py", "tests/test_utils.py"]
    assert rt.select_test_files("requests/models.py", files, {}, max_files=3) == ["test_requests.py"]
    assert rt.select_test_files("src/_pytest/python.py", ["testing/test_pytest.py"], {}, 3) == \
        ["testing/test_pytest.py"]


def test_select_ignores_same_name_in_unrelated_subpackages():
    files = ["astropy/cosmology/tests/test_core.py", "astropy/timeseries/tests/test_sampled.py",
             "astropy/units/tests/test_quantity.py", "astropy/units/tests/test_quantity_ufuncs.py"]
    # core.py is generic: no test_core.py from another subpackage, but the
    # timeseries area matches its own tests folder
    assert rt.select_test_files("astropy/timeseries/core.py", files, {}, 3) == \
        ["astropy/timeseries/tests/test_sampled.py"]
    got = rt.select_test_files("astropy/units/quantity.py", files, {}, 3, "Quantity.__array_ufunc__")
    assert got[:2] == ["astropy/units/tests/test_quantity.py", "astropy/units/tests/test_quantity_ufuncs.py"]


def test_select_matches_area_and_fuzzy_module_names():
    files = ["tests/test_ext_autodoc_configs.py", "tests/test_build_html.py", "tests/test_pycode_ast.py"]
    assert rt.select_test_files("sphinx/ext/autodoc/__init__.py", files, {}, 3)[0] == \
        "tests/test_ext_autodoc_configs.py"
    assert rt.select_test_files("sphinx/pycode/ast.py", files, {}, 3) == ["tests/test_pycode_ast.py"]
    files = ["astropy/coordinates/tests/test_intermediate_transformations.py",
             "astropy/coordinates/tests/test_sky_coord.py"]
    assert rt.select_test_files("astropy/coordinates/builtin_frames/intermediate_rotation_transforms.py",
                                files, {}, 3, "the function handling this error")[0] == files[0]
    files = ["tests/model_fields/test_jsonfield.py", "tests/serializers/test_json.py",
             "tests/backends/tests.py"]
    got = rt.select_test_files("django/db/models/fields/json.py", files, {}, 3, "KeyTransform.as_sql")
    assert got[0] == "tests/serializers/test_json.py"          # the module's own name wins
    assert "tests/model_fields/test_jsonfield.py" in got and "tests/backends/tests.py" not in got


def test_package_file_only_when_nothing_else_matches():
    files = ["tests/migrations/test_loader.py", "tests/template_backends/test_django.py"]
    assert rt.select_test_files("django/db/migrations/loader.py", files, {}, 3) == \
        ["tests/migrations/test_loader.py"]


def test_django_labels_and_commands():
    assert rt.django_label("tests/queries/test_query.py") == "queries.test_query"
    assert rt.django_label("django/test/utils.py") is None
    cmd = rt.test_command("django", ["tests/queries/tests.py"])
    assert cmd.startswith("./tests/runtests.py") and cmd.endswith("queries.tests")
    assert rt.test_command("django", ["django/x.py"]) is None
    assert rt.test_command("pytest", ["a/test_b.py"]).startswith("pytest -rA")
    assert "bin/test" in rt.test_command("sympy", ["sympy/core/tests/test_expr.py"])


def test_parse_pytest():
    log = ("PASSED a/test_x.py::test_one\nFAILED a/test_x.py::test_two - AssertionError: x\n"
           "\x1b[31mERROR a/test_y.py - ImportError: boom\x1b[0m\nSKIPPED [1] a/test_x.py:9: skip\n")
    got = rt.parse_results(log, "pytest")
    assert got["a/test_x.py::test_one"] == "PASSED"
    assert got["a/test_x.py::test_two"] == "FAILED"
    assert got["a/test_y.py"] == "ERROR"


def test_parse_django_including_docstring_lines():
    log = ("test_a (queries.tests.T) ... ok\n"
           "test_b (queries.tests.T)\nChecks something. ... FAIL\n"
           "test_c (queries.tests.T) ... skipped 'no db'\n"
           "ERROR: test_d (queries.tests.T)\n")
    got = rt.parse_results(log, "django")
    assert got == {"test_a (queries.tests.T)": "PASSED", "test_b (queries.tests.T)": "FAILED",
                   "test_c (queries.tests.T)": "SKIPPED", "test_d (queries.tests.T)": "ERROR"}


def test_parse_sympy_keys_by_file():
    log = ("sympy/core/tests/test_expr.py[3] \ntest_a ok\ntest_b F\n"
           "sympy/core/tests/test_basic.py[1] \ntest_a E\n")
    got = rt.parse_results(log, "sympy")
    assert got == {"sympy/core/tests/test_expr.py::test_a": "PASSED",
                   "sympy/core/tests/test_expr.py::test_b": "FAILED",
                   "sympy/core/tests/test_basic.py::test_a": "ERROR"}


def test_newly_failing_ignores_old_failures_and_counts_missing():
    before = {"t1": "PASSED", "t2": "FAILED", "t3": "PASSED", "t4": "XFAIL"}
    after = {"t1": "PASSED", "t2": "FAILED", "t4": "XFAIL"}       # t3 vanished (import error)
    assert rt.newly_failing(before, after) == ["t3"]
    assert rt.newly_failing(before, {**after, "t3": "FAILED", "t2": "PASSED"}) == ["t3"]


def test_failure_excerpt_drops_passing_lines():
    log = "PASSED a::t1\nFAILED a::t2 - boom\nTraceback line\n"
    out = rt.failure_excerpt(log)
    assert "t1" not in out and "boom" in out and "Traceback" in out


# ── the sandbox side, with a fake sandbox ─────────────────────────────────

class FakeSandbox:
    """Answers git ls-files / grep, and test runs from a per-content script."""

    def __init__(self, ls: str, grep: str, runs: dict[str, str]):
        self.ls, self.grep, self.runs = ls, grep, runs
        self.files: dict[str, str] = {}
        self.commands: list[str] = []

    async def write_file(self, path, content):
        self.files[path] = content

    async def run(self, command, timeout=180, trim=True):
        self.commands.append(command)
        if command.startswith("git ls-files -- "):
            return 0, self.ls
        if "grep -c -w" in command:
            return 0, self.grep
        key = next((k for k in self.runs if k in self.files.get("pkg/mod.py", "ORIGINAL")), "ORIGINAL")
        return 1, self.runs[key]

    async def close(self):
        pass


def test_find_test_files_uses_listing_and_grep():
    sb = FakeSandbox(ls="pkg/mod.py\npkg/tests/test_mod.py\npkg/tests/test_other.py\nREADME.py\n",
                     grep="pkg/tests/test_other.py:4\npkg/mod.py:7\n", runs={})
    got = asyncio.run(rt.find_test_files(sb, "pkg/mod.py", "Thing.method_name", max_files=3))
    assert got == ["pkg/tests/test_mod.py", "pkg/tests/test_other.py"]


# ── the retry loop in the fix agent ───────────────────────────────────────

ORIGINAL = "def f():\n    return 1  # ORIGINAL\n"
BASELINE = "PASSED pkg/tests/test_mod.py::test_a\nPASSED pkg/tests/test_mod.py::test_b\n"


def _agent(tmp_path, sandbox, **settings):
    from app.agents.fix_generation import FixGenerationAgent
    cand = tmp_path / "fix"
    shutil.copytree(DEFAULT_ROOT / "fix", cand)
    s = json.loads((cand / "settings.json").read_text())
    s.update({"test_mode": "existing_tests", **settings})
    (cand / "settings.json").write_text(json.dumps(s))
    agent = FixGenerationAgent.__new__(FixGenerationAgent)
    agent._harness = load_harness("fix", cand)
    agent._owner, agent._repo, agent._sandbox = "acme", "pkg", sandbox
    return agent


def _run(agent, retries):
    calls = []

    async def fake_generate(content, fn, incident, path, bundle, test_failures=""):
        calls.append(test_failures)
        return retries.pop(0)

    agent._generate_fix = fake_generate
    steps: list[str] = []
    out = asyncio.run(agent._existing_tests_retry(
        None, "pkg/mod.py", "f", ORIGINAL, None,
        ORIGINAL, "def f():\n    return 2  # FIRST\n", [], steps))
    return out, steps, calls


def test_retry_regenerates_on_regression_and_keeps_the_clean_fix(tmp_path):
    sb = FakeSandbox(ls="pkg/mod.py\npkg/tests/test_mod.py\n", grep="", runs={
        "ORIGINAL": BASELINE,
        "FIRST": "PASSED pkg/tests/test_mod.py::test_a\nFAILED pkg/tests/test_mod.py::test_b - boom\n",
        "SECOND": BASELINE,
    })
    agent = _agent(tmp_path, sb)
    second = (ORIGINAL, "def f():\n    return 3  # SECOND\n", [], None)
    (old, new, patches), steps, calls = _run(agent, [second])
    assert "SECOND" in new
    assert len(calls) == 1 and "test_b" in calls[0] and "boom" in calls[0] and "FIRST" in calls[0]
    assert [r["run"] for r in agent._test_log] == ["before", "attempt 1", "attempt 2"]
    assert agent._test_log[1]["newly_failing"] == ["pkg/tests/test_mod.py::test_b"]
    assert any("still pass" in s for s in steps)


def test_all_attempts_break_tests_submits_fewest_broken(tmp_path):
    sb = FakeSandbox(ls="pkg/tests/test_mod.py\n", grep="", runs={
        "ORIGINAL": BASELINE,
        "FIRST": "FAILED pkg/tests/test_mod.py::test_b - x\nPASSED pkg/tests/test_mod.py::test_a\n",
        "SECOND": "FAILED pkg/tests/test_mod.py::test_a - x\nFAILED pkg/tests/test_mod.py::test_b - x\n",
    })
    agent = _agent(tmp_path, sb, test_max_attempts=2)
    (_, new, _), steps, calls = _run(agent, [(ORIGINAL, "def f():\n    return 3  # SECOND\n", [], None)])
    assert "FIRST" in new and len(calls) == 1

    agent = _agent(tmp_path / "b", FakeSandbox(sb.ls, "", sb.runs), test_max_attempts=2, test_pick="last")
    (_, new, _), _, _ = _run(agent, [(ORIGINAL, "def f():\n    return 3  # SECOND\n", [], None)])
    assert "SECOND" in new


def test_unusable_baseline_or_no_tests_submits_untested(tmp_path):
    sb = FakeSandbox(ls="pkg/mod.py\n", grep="", runs={"ORIGINAL": BASELINE})
    agent = _agent(tmp_path, sb)
    (_, new, _), steps, calls = _run(agent, [])
    assert "FIRST" in new and not calls and "no existing tests" in steps[-1]

    sb = FakeSandbox(ls="pkg/tests/test_mod.py\n", grep="", runs={"ORIGINAL": "nothing parsable\n"})
    agent = _agent(tmp_path / "b", sb)
    (_, new, _), steps, calls = _run(agent, [])
    assert "FIRST" in new and not calls and "baseline run unusable" in steps[-1]


def test_retry_with_no_edit_keeps_best(tmp_path):
    sb = FakeSandbox(ls="pkg/tests/test_mod.py\n", grep="", runs={
        "ORIGINAL": BASELINE,
        "FIRST": "FAILED pkg/tests/test_mod.py::test_b - x\nPASSED pkg/tests/test_mod.py::test_a\n",
    })
    agent = _agent(tmp_path, sb)
    (_, new, _), steps, _ = _run(agent, [("", "", [], None)])
    assert "FIRST" in new and "made no edit" in steps[-1]


def test_default_harness_has_test_mode_off_and_valid_choices():
    h = load_harness("fix")
    assert h.setting("test_mode") == "off"
    from app.harness_optimizer import candidates, profiles
    profiles.use("fix")
    try:
        bounds = profiles.active().setting_bounds
        assert h.setting("test_mode") in bounds["test_mode"]
        assert h.setting("test_pick") in bounds["test_pick"]
    finally:
        profiles.use("diagnosis")
