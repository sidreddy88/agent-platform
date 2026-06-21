"""
Code graph — in-memory call graph with forward and reverse indices.

Data structure: two hash maps of sets.
  forward[caller_name]  = {callee_name, ...}   — "what does this function call?"
  reverse[callee_name]  = [CallerInfo, ...]    — "what calls this function?" (O(1))

The reverse index is the critical structure: before the FixGenerationAgent patches
a function, it calls find_callers() to discover every caller in the codebase.
Those callers go into the fix prompt so the agent can update them in the same PR.

Lifecycle:
  1. index_directory(path) — parse all JS/TS files, build graph, persist to Postgres
  2. load_from_store()     — on startup, load Postgres rows into memory (skips re-parse)
  3. find_callers(name)    — O(1) reverse lookup, called by FixGenerationAgent tool

Thread safety: the graph is built once and then read-only during request handling.
Incremental updates (file change → re-parse → patch graph) are not yet implemented.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# File extensions the tree-sitter parser supports
_JS_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx"}

# Directories to skip when walking the codebase
_SKIP_DIRS = {
    "node_modules", "__pycache__", ".git", "dist", "build",
    ".next", "coverage", ".cache", "vendor",
}


@dataclass
class CallerInfo:
    """A single caller of a function — one entry in the reverse index."""
    file_path: str       # repo-relative path of the file containing the call
    function_name: str   # name of the function that makes the call
    line: int            # line number of the call expression


@dataclass
class CodeGraph:
    """
    In-memory call graph for a JS/TS codebase.

    forward: dict[str, set[str]]
        Maps caller_name → set of callee names. Answers: "what does foo call?"

    reverse: dict[str, list[CallerInfo]]
        Maps callee_name → list of CallerInfo. Answers: "what calls foo?" (O(1))

    _edges: list[dict]
        Raw edge list produced during build — used for bulk persistence to Postgres.
        Format: {caller_file, caller_function, callee_name, line}
    """
    forward: dict[str, set[str]] = field(default_factory=dict)
    reverse: dict[str, list[CallerInfo]] = field(default_factory=dict)
    _edges: list[dict] = field(default_factory=list)

    # -------------------------------------------------------------------------
    # Build from source files
    # -------------------------------------------------------------------------

    @classmethod
    def build_from_directory(cls, root: str) -> "CodeGraph":
        """Parse all JS/TS files under `root` and build the call graph.

        Skips node_modules and other non-source directories.
        Returns an empty graph (with a warning) if tree-sitter is unavailable.
        """
        try:
            from app.services.code_graph.parser import (
                extract_call_sites,
                extract_function_definitions,
                parse_file,
            )
        except ImportError:
            logger.warning("[CodeGraph] tree-sitter not available — returning empty graph")
            return cls()

        graph = cls()
        root_path = Path(root)
        files_parsed = 0
        parse_errors = 0

        for file_path in root_path.rglob("*"):
            # Skip directories and non-JS/TS files
            if not file_path.is_file():
                continue
            if file_path.suffix not in _JS_EXTENSIONS:
                continue
            # Skip any path that contains a blacklisted directory segment
            if any(part in _SKIP_DIRS for part in file_path.parts):
                continue

            try:
                tree, source = parse_file(str(file_path))
                call_sites = extract_call_sites(tree, source)

                # Use a path relative to root for portability
                try:
                    rel_path = str(file_path.relative_to(root_path))
                except ValueError:
                    rel_path = str(file_path)

                for site in call_sites:
                    graph._add_edge(
                        caller_file=rel_path,
                        caller_function=site.caller_name,
                        callee_name=site.callee_name,
                        line=site.line,
                    )

                files_parsed += 1

            except Exception as exc:
                parse_errors += 1
                logger.debug("[CodeGraph] parse error in %s: %s", file_path, exc)

        logger.info(
            "[CodeGraph] built from %d files — %d edges, %d parse errors",
            files_parsed, len(graph._edges), parse_errors,
        )
        return graph

    # -------------------------------------------------------------------------
    # Load from Postgres (startup path — skips re-parsing)
    # -------------------------------------------------------------------------

    @classmethod
    def load_from_store(cls) -> "CodeGraph":
        """Rebuild the in-memory graph from persisted Postgres rows.

        Called on server startup so agents can query the graph immediately
        without waiting for a full re-parse of the codebase.
        Returns an empty graph if the table is empty or the DB is unavailable.
        """
        try:
            from app.services.code_graph.store import load_edges
            edges = load_edges()
        except Exception as exc:
            logger.warning("[CodeGraph] could not load from store: %s", exc)
            return cls()

        graph = cls()
        for e in edges:
            graph._add_edge(
                caller_file=e["caller_file"],
                caller_function=e["caller_function"],
                callee_name=e["callee_name"],
                line=e["line"],
            )

        logger.info("[CodeGraph] loaded %d edges from Postgres", len(graph._edges))
        return graph

    # -------------------------------------------------------------------------
    # Query API — used by agents
    # -------------------------------------------------------------------------

    def find_callers(self, function_name: str) -> list[CallerInfo]:
        """Return every function that calls `function_name`.

        O(1) reverse index lookup. Returns an empty list if the function
        has no known callers (not in the index, or never called).
        """
        return self.reverse.get(function_name, [])

    def find_callees(self, function_name: str) -> list[str]:
        """Return every function name that `function_name` calls.

        O(1) forward index lookup.
        """
        return sorted(self.forward.get(function_name, set()))

    def stats(self) -> dict:
        """Summary statistics for logging and the index script output."""
        all_callees = set(self.reverse.keys())
        all_callers = set(self.forward.keys())

        # Functions with the most callers — useful for sanity-checking the index
        top_callers = sorted(
            [(name, len(callers)) for name, callers in self.reverse.items()],
            key=lambda x: x[1],
            reverse=True,
        )[:10]

        return {
            "total_edges": len(self._edges),
            "unique_callers": len(all_callers),
            "unique_callees": len(all_callees),
            "top_called_functions": top_callers,
        }

    # -------------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------------

    def _add_edge(
        self,
        caller_file: str,
        caller_function: str,
        callee_name: str,
        line: int,
    ) -> None:
        """Add one directed edge to both the forward and reverse indices."""
        # Forward index: caller_function → {callee_name}
        if caller_function not in self.forward:
            self.forward[caller_function] = set()
        self.forward[caller_function].add(callee_name)

        # Reverse index: callee_name → [CallerInfo]
        if callee_name not in self.reverse:
            self.reverse[callee_name] = []
        self.reverse[callee_name].append(CallerInfo(
            file_path=caller_file,
            function_name=caller_function,
            line=line,
        ))

        # Raw edge list for Postgres persistence
        self._edges.append({
            "caller_file":     caller_file,
            "caller_function": caller_function,
            "callee_name":     callee_name,
            "line":            line,
        })
