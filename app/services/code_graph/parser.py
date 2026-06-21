"""
Code graph — Phase 1: tree-sitter parser.

Parses JS/TS source files and extracts two things:
  1. FunctionDef  — where each function is defined (name, location, kind, export status)
  2. CallSite     — where each function is called from (caller → callee edges)

Together these produce the raw edge list that graph.py turns into a call graph.

Supported function kinds:
  function  — `function foo() {}`
  arrow     — `const foo = () => {}`
  method    — `class Foo { bar() {} }`

Usage:
    from app.services.code_graph.parser import parse_file, extract_function_definitions, extract_call_sites
    tree, source = parse_file("routes/services/image.js")
    fns   = extract_function_definitions(tree, source)
    sites = extract_call_sites(tree, source)
    for s in sites:
        print(s.caller_name, "→", s.callee_name, "at line", s.line)
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from pathlib import Path

import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
from tree_sitter import Language, Node, Parser

# ---------------------------------------------------------------------------
# Language + parser singletons
#
# tree-sitter Language objects wrap the compiled C grammar for each language.
# Parser objects are stateless after construction — safe to share across calls.
# We create one parser per file extension and reuse them for the process lifetime.
# ---------------------------------------------------------------------------

_JS_LANGUAGE = Language(tsjs.language())
_TS_LANGUAGE = Language(tsts.language_typescript())
_TSX_LANGUAGE = Language(tsts.language_tsx())

# Map file extension → parser. .jsx uses the JS grammar (JSX is a JS superset).
_PARSERS: dict[str, Parser] = {
    ".js":  Parser(_JS_LANGUAGE),
    ".jsx": Parser(_JS_LANGUAGE),
    ".ts":  Parser(_TS_LANGUAGE),
    ".tsx": Parser(_TSX_LANGUAGE),
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class FunctionDef:
    """A single function definition extracted from a JS/TS file."""
    name: str
    start_line: int    # 1-indexed, inclusive
    end_line: int      # 1-indexed, inclusive
    start_byte: int    # byte offset in the source file (used for containment checks)
    end_byte: int
    kind: str          # "function" | "arrow" | "method"
    is_exported: bool  # True if preceded by `export` keyword


@dataclass
class CallSite:
    """A single call expression, tagged with the function that contains it."""
    caller_name: str   # name of the enclosing function (top-level only)
    callee_name: str   # simple name of the function being called (e.g. "classifyFields")
    line: int          # 1-indexed line number of the call expression


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_file(path: str) -> tuple["tree_sitter.Tree", str]:
    """Parse a JS/TS file and return (syntax_tree, source_text).

    Reads the file as bytes so tree-sitter byte offsets in node.start_byte /
    node.end_byte stay consistent with node.text. The returned source string
    is decoded for display only — never use it for byte slicing.
    """
    p = Path(path)
    source_bytes = p.read_bytes()
    parser = _PARSERS.get(p.suffix)
    if parser is None:
        raise ValueError(f"Unsupported extension: {p.suffix}")
    # parser.parse() produces a Concrete Syntax Tree (CST) — every token is
    # represented, including whitespace and punctuation. Unlike an AST, nothing
    # is dropped, which makes error recovery and incremental re-parsing possible.
    tree = parser.parse(source_bytes)
    return tree, source_bytes.decode("utf-8", errors="ignore")


def extract_function_definitions(tree: "tree_sitter.Tree", source: str) -> list[FunctionDef]:
    """Walk the CST and return all top-level function definitions in the file.

    Top-level means: not nested inside another function. Nested functions are
    skipped intentionally — they're implementation details of their parent and
    would create noise in the call graph.
    """
    results: list[FunctionDef] = []
    _walk(tree.root_node, source, results, exported=False)
    return results


def extract_call_sites(tree: "tree_sitter.Tree", source: str) -> list[CallSite]:
    """Return all call expressions in the file, each tagged with its enclosing function.

    Algorithm — byte-range containment:
      1. Collect all top-level FunctionDef objects (already have start_byte/end_byte).
      2. Walk the entire CST collecting every call_expression node.
      3. For each call expression, binary-search the sorted FunctionDef list to find
         which function contains it (start_byte <= call_byte <= end_byte).
      4. Emit a CallSite(caller_name, callee_name, line).

    Call sites outside any function (module-level code) are skipped — we only
    care about function-to-function edges for the call graph.

    Callee name extraction:
      foo()         → "foo"         (identifier node)
      obj.foo()     → "foo"         (member_expression — take the rightmost property)
      foo.bar.baz() → "baz"         (nested member_expression — still rightmost)
      foo()()       → skipped       (dynamic call — callee is a call_expression)
    """
    # Step 1: get all function definitions sorted by start_byte for binary search
    fn_defs = extract_function_definitions(tree, source)
    if not fn_defs:
        return []

    # Sort by start_byte (they're usually already in order, but guarantee it)
    fn_defs_sorted = sorted(fn_defs, key=lambda f: f.start_byte)
    start_bytes = [f.start_byte for f in fn_defs_sorted]

    # Step 2: collect all call_expression nodes by walking the full tree
    call_nodes: list[Node] = []
    _collect_calls(tree.root_node, call_nodes)

    # Step 3 + 4: for each call, find its containing function and extract callee name
    results: list[CallSite] = []
    for call_node in call_nodes:
        callee_name = _extract_callee_name(call_node)
        if callee_name is None:
            continue  # dynamic or computed call — skip

        # Binary search: find the last function that starts at or before this call
        idx = bisect.bisect_right(start_bytes, call_node.start_byte) - 1
        if idx < 0:
            continue  # call is before the first function — module-level, skip

        containing_fn = fn_defs_sorted[idx]
        if call_node.start_byte > containing_fn.end_byte:
            continue  # call is after the function ends — between functions, skip

        results.append(CallSite(
            caller_name=containing_fn.name,
            callee_name=callee_name,
            line=call_node.start_point[0] + 1,  # tree-sitter is 0-indexed
        ))

    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _walk(node: Node, source: str, out: list[FunctionDef], exported: bool) -> None:
    """Recursively walk the CST collecting top-level function definitions.

    The three JS/TS function forms we handle:
      1. function_declaration  — `function foo() {}`
      2. lexical_declaration   — `const foo = () => {}` or `const foo = function() {}`
      3. method_definition     — `class Foo { bar() {} }`

    We stop recursing when we enter a function body (return early) so nested
    functions are not collected — only the outermost definition per name.
    """

    if node.type == "export_statement":
        # `export function foo()` or `export const foo = () => {}`
        # Everything directly under an export_statement is exported.
        # We pass exported=True down to the children so they record it.
        for child in node.children:
            _walk(child, source, out, exported=True)
        return

    if node.type == "function_declaration":
        # `function foo() { ... }`
        # The identifier is the first child of type "identifier".
        name = _child_text(node, "identifier", source)
        if name:
            out.append(FunctionDef(
                name=name,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                start_byte=node.start_byte,
                end_byte=node.end_byte,
                kind="function",
                is_exported=exported,
            ))
        # Stop here — don't recurse into the body so nested functions are skipped
        return

    if node.type in ("lexical_declaration", "variable_declaration"):
        # `const foo = () => {}` or `const foo = function() {}`
        # A lexical_declaration has one or more variable_declarator children.
        # Each declarator has a "name" field (identifier) and a "value" field (the RHS).
        for decl in node.children:
            if decl.type == "variable_declarator":
                name_node = decl.child_by_field_name("name")
                value_node = decl.child_by_field_name("value")
                # Only record it if the RHS is actually a function (not a string, object, etc.)
                if name_node and value_node and value_node.type in ("arrow_function", "function"):
                    out.append(FunctionDef(
                        name=name_node.text.decode(),
                        start_line=decl.start_point[0] + 1,
                        end_line=decl.end_point[0] + 1,
                        start_byte=decl.start_byte,
                        end_byte=decl.end_byte,
                        kind="arrow" if value_node.type == "arrow_function" else "function",
                        is_exported=exported,
                    ))
        return

    if node.type == "method_definition":
        # `class Foo { bar() {} }`
        # property_identifier is the method name node type in tree-sitter's JS grammar.
        name = _child_text(node, "property_identifier", source)
        # Skip constructors — they're not independently callable by name
        if name and name not in ("constructor",):
            out.append(FunctionDef(
                name=name,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                start_byte=node.start_byte,
                end_byte=node.end_byte,
                kind="method",
                is_exported=exported,
            ))
        return

    # For all other node types (program, class_declaration, class_body, if_statement, etc.)
    # keep recursing — we haven't entered a function body yet.
    for child in node.children:
        _walk(child, source, out, exported=exported)


def _collect_calls(node: Node, out: list[Node]) -> None:
    """Walk the full CST and collect every call_expression node.

    We collect ALL call expressions (including those inside function bodies,
    conditionals, loops, etc.) because step 3 in extract_call_sites will
    filter by containment — only calls inside a known function are kept.
    """
    if node.type == "call_expression":
        out.append(node)
        # Still recurse — a call can contain another call: foo(bar())
    for child in node.children:
        _collect_calls(child, out)


def _extract_callee_name(call_node: Node) -> str | None:
    """Extract the simple callee name from a call_expression node.

    Returns None for dynamic or computed callees that can't be statically resolved.

    Examples:
      foo()           → "foo"
      obj.foo()       → "foo"   (member_expression, take rightmost property)
      foo.bar.baz()   → "baz"   (nested members, still rightmost)
      arr[0]()        → None    (subscript call — dynamic)
      (fn || noop)()  → None    (expression call — dynamic)
    """
    # The "function" field of call_expression is the callee expression
    callee = call_node.child_by_field_name("function")
    if callee is None:
        return None

    if callee.type == "identifier":
        # Simple call: foo()
        return callee.text.decode()

    if callee.type == "member_expression":
        # Dotted call: obj.foo() or foo.bar.baz()
        # The "property" field is always the rightmost identifier
        prop = callee.child_by_field_name("property")
        if prop and prop.type in ("property_identifier", "identifier"):
            return prop.text.decode()

    # Anything else (subscript, call expression, parenthesized expression, etc.)
    # is a dynamic call that can't be statically resolved — skip it.
    return None


def _child_text(node: Node, child_type: str, source: str) -> str | None:
    """Return the decoded text of the first direct child with the given node type.

    Uses node.text (a byte slice from the original parse buffer) rather than
    slicing the source string by character index. These diverge when the file
    contains multi-byte UTF-8 characters (e.g. emoji in comments) before this node.
    """
    for child in node.children:
        if child.type == child_type:
            return child.text.decode()
    return None
