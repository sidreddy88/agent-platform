"""
Code graph — Phase 1: tree-sitter parser.

Parses JS/TS source files and extracts function definitions.
Each FunctionDef records the name, location, kind, and whether it is exported.

Supported kinds:
  function    — `function foo() {}`
  arrow       — `const foo = () => {}`
  method      — `class Foo { bar() {} }`

Usage:
    from app.services.code_graph.parser import parse_file, extract_function_definitions
    tree = parse_file("routes/services/image.js")
    fns  = extract_function_definitions(tree, source)
    for f in fns:
        print(f.name, f.start_line, f.end_line, f.kind, f.is_exported)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
from tree_sitter import Language, Node, Parser

# ---------------------------------------------------------------------------
# Language objects (module-level singletons — parsers are cheap to share)
# ---------------------------------------------------------------------------

_JS_LANGUAGE = Language(tsjs.language())
_TS_LANGUAGE = Language(tsts.language_typescript())
_TSX_LANGUAGE = Language(tsts.language_tsx())

_PARSERS: dict[str, Parser] = {
    ".js":  Parser(_JS_LANGUAGE),
    ".jsx": Parser(_JS_LANGUAGE),
    ".ts":  Parser(_TS_LANGUAGE),
    ".tsx": Parser(_TSX_LANGUAGE),
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class FunctionDef:
    name: str
    start_line: int   # 1-indexed
    end_line: int     # 1-indexed, inclusive
    start_byte: int
    end_byte: int
    kind: str         # "function" | "arrow" | "method"
    is_exported: bool


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_file(path: str) -> tuple["tree_sitter.Tree", str]:
    """Parse a JS/TS file. Returns (tree, source_text).

    Parses from bytes so tree-sitter's byte offsets stay consistent with
    node.text. The returned source string is for display only — never slice
    it with start_byte/end_byte, use node.text.decode() instead.
    """
    p = Path(path)
    source_bytes = p.read_bytes()
    parser = _PARSERS.get(p.suffix)
    if parser is None:
        raise ValueError(f"Unsupported extension: {p.suffix}")
    tree = parser.parse(source_bytes)
    return tree, source_bytes.decode("utf-8", errors="ignore")


def extract_function_definitions(tree: "tree_sitter.Tree", source: str) -> list[FunctionDef]:
    """Walk the CST and return all function definitions in the file."""
    results: list[FunctionDef] = []
    _walk(tree.root_node, source, results, exported=False)
    return results


# ---------------------------------------------------------------------------
# Internal tree walk
# ---------------------------------------------------------------------------

def _walk(node: Node, source: str, out: list[FunctionDef], exported: bool) -> None:
    """Recursively walk the CST, collecting function definitions."""

    if node.type == "export_statement":
        # Everything directly inside an export_statement is exported
        for child in node.children:
            _walk(child, source, out, exported=True)
        return

    if node.type == "function_declaration":
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
        # Don't recurse into the body — nested functions are a separate concern
        return

    if node.type in ("lexical_declaration", "variable_declaration"):
        # const foo = () => {} or const foo = function() {}
        for decl in node.children:
            if decl.type == "variable_declarator":
                name_node = decl.child_by_field_name("name")
                value_node = decl.child_by_field_name("value")
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
        name = _child_text(node, "property_identifier", source)
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

    # Recurse into all other node types (program, class_body, etc.)
    for child in node.children:
        _walk(child, source, out, exported=exported)


def _child_text(node: Node, child_type: str, source: str) -> str | None:
    """Return the text of the first direct child with the given node type.

    Uses node.text (byte slice from the parsed buffer) rather than
    source[start_byte:end_byte] (character slice). The two diverge when
    the file contains multi-byte UTF-8 characters before this node.
    """
    for child in node.children:
        if child.type == child_type:
            return child.text.decode()
    return None
