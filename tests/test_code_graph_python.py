"""
Tests for Python support in the tree-sitter call-graph parser.

Why this exists: a SWE-bench retrieval-path audit found `find_callers`
returning "no callers found / index not built" on all 100 instances of
app/evals/swebench_verified_sample.jsonl. The sample is 100% Python and
app/services/code_graph/parser.py only loaded the JavaScript and TypeScript
grammars, so every file was skipped in silence. The call graph was
structurally dead for that entire evaluation, and the failure mode read like
a fact about the code rather than a missing grammar.

These cover the Python branch and assert the JS branch still behaves, since
both now share extract_function_definitions / extract_call_sites via
dispatch on tree.language.
"""
from __future__ import annotations

from app.services.code_graph.parser import (
    extract_call_sites,
    extract_function_definitions,
    parse_source,
)

PY_SAMPLE = '''\
import os


def top_level(a, b):
    return helper(a) + os.path.join(b)


@decorator
def decorated():
    obj.method_call()
    return 1


def _private():
    pass


class Foo:
    def __init__(self):
        self.x = setup()

    def method(self):
        return self.other()

    @property
    def prop(self):
        return compute()


async def async_fn():
    await thing()


def outer():
    def nested():
        inner_call()
    return nested
'''


def _py_tree():
    return parse_source(PY_SAMPLE.encode(), ".py")


# ---------------------------------------------------------------------------
# Function definitions
# ---------------------------------------------------------------------------

def test_collects_module_level_functions():
    names = {f.name for f in extract_function_definitions(_py_tree(), PY_SAMPLE)}
    assert {"top_level", "decorated", "_private", "async_fn", "outer"} <= names


def test_decorated_function_is_collected():
    """`decorated_definition` wraps the def — must recurse, not treat as a leaf."""
    fns = {f.name: f for f in extract_function_definitions(_py_tree(), PY_SAMPLE)}
    assert "decorated" in fns
    assert fns["decorated"].kind == "function"


def test_class_methods_are_collected_and_tagged():
    fns = {f.name: f for f in extract_function_definitions(_py_tree(), PY_SAMPLE)}
    assert fns["method"].kind == "method"
    assert fns["prop"].kind == "method"      # decorated method still a method
    assert fns["top_level"].kind == "function"


def test_dunder_init_is_skipped():
    """Parity with the JS branch skipping `constructor` — not callable by name."""
    names = {f.name for f in extract_function_definitions(_py_tree(), PY_SAMPLE)}
    assert "__init__" not in names


def test_nested_function_is_not_collected():
    """Nested defs are implementation details of their parent, same rule as JS."""
    names = {f.name for f in extract_function_definitions(_py_tree(), PY_SAMPLE)}
    assert "nested" not in names


def test_is_exported_follows_underscore_convention():
    """Python has no `export` keyword; a leading underscore means private."""
    fns = {f.name: f for f in extract_function_definitions(_py_tree(), PY_SAMPLE)}
    assert fns["top_level"].is_exported is True
    assert fns["_private"].is_exported is False


# ---------------------------------------------------------------------------
# Call sites
# ---------------------------------------------------------------------------

def test_plain_and_attribute_calls_resolve_to_rightmost_name():
    edges = {(c.caller_name, c.callee_name) for c in extract_call_sites(_py_tree(), PY_SAMPLE)}
    assert ("top_level", "helper") in edges          # identifier
    assert ("top_level", "join") in edges            # os.path.join -> "join"
    assert ("decorated", "method_call") in edges     # obj.method_call()
    assert ("method", "other") in edges              # self.other()


def test_async_body_calls_are_captured():
    edges = {(c.caller_name, c.callee_name) for c in extract_call_sites(_py_tree(), PY_SAMPLE)}
    assert ("async_fn", "thing") in edges


def test_nested_function_calls_roll_up_to_enclosing_top_level():
    """`nested` isn't a collected function, so its calls attribute to `outer`.

    Same containment behaviour as the JS branch — byte-range lookup finds the
    innermost *collected* function, and nested defs are never collected.
    """
    edges = {(c.caller_name, c.callee_name) for c in extract_call_sites(_py_tree(), PY_SAMPLE)}
    assert ("outer", "inner_call") in edges


def test_module_level_calls_are_skipped():
    """Only function-to-function edges belong in the call graph."""
    src = "import os\nos.makedirs('x')\n"
    tree = parse_source(src.encode(), ".py")
    assert extract_call_sites(tree, src) == []


# ---------------------------------------------------------------------------
# JS branch unchanged
# ---------------------------------------------------------------------------

JS_SAMPLE = '''\
export function outerJs(a) {
  return helperJs(a);
}

const arrowJs = (b) => {
  obj.doThing(b);
};

class Bar {
  constructor() {}
  methodJs() {
    return this.otherJs();
  }
}
'''


def test_js_branch_still_works_after_python_dispatch_added():
    tree = parse_source(JS_SAMPLE.encode(), ".js")
    fns = {f.name: f for f in extract_function_definitions(tree, JS_SAMPLE)}
    assert fns["outerJs"].kind == "function"
    assert fns["outerJs"].is_exported is True
    assert fns["arrowJs"].kind == "arrow"
    assert fns["methodJs"].kind == "method"
    assert "constructor" not in fns

    edges = {(c.caller_name, c.callee_name) for c in extract_call_sites(tree, JS_SAMPLE)}
    assert ("outerJs", "helperJs") in edges
    assert ("arrowJs", "doThing") in edges
    assert ("methodJs", "otherJs") in edges


# ---------------------------------------------------------------------------
# Unsupported-language warning
#
# The silent skip is what let the JS-only limitation survive a full
# 100-instance evaluation, so the warning is the actual fix for the class of
# bug — Python support only fixes this instance of it.
# ---------------------------------------------------------------------------

def test_warns_when_nothing_parsed_but_source_was_skipped(tmp_path, caplog):
    import logging

    from app.services.code_graph.graph import CodeGraph

    (tmp_path / "main.go").write_text("package main\nfunc main() {}\n")
    (tmp_path / "util.go").write_text("package main\nfunc util() {}\n")
    (tmp_path / "README.md").write_text("# not code\n")

    with caplog.at_level(logging.WARNING):
        graph = CodeGraph.build_from_directory(str(tmp_path))

    assert graph.forward == {}
    msg = caplog.text
    assert "parsed 0 files" in msg
    assert ".go (2)" in msg
    # Markdown is not code — must not be counted as a coverage gap.
    assert ".md" not in msg


def test_warns_when_graph_covers_only_a_minority_of_the_repo(tmp_path, caplog):
    import logging

    from app.services.code_graph.graph import CodeGraph

    (tmp_path / "app.py").write_text("def a():\n    b()\n")
    for i in range(3):
        (tmp_path / f"svc{i}.go").write_text("package main\n")

    with caplog.at_level(logging.WARNING):
        CodeGraph.build_from_directory(str(tmp_path))

    msg = caplog.text
    assert "covers only part of this repo" in msg


def test_no_warning_for_a_clean_python_repo(tmp_path, caplog):
    import logging

    from app.services.code_graph.graph import CodeGraph

    (tmp_path / "app.py").write_text("def a():\n    return b()\n")
    (tmp_path / "README.md").write_text("# docs\n")

    with caplog.at_level(logging.WARNING):
        graph = CodeGraph.build_from_directory(str(tmp_path))

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert graph.forward  # Python files actually produced edges
