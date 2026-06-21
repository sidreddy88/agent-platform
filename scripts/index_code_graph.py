"""
CLI — bulk index the TargetApp codebase into the call graph.

Usage:
    python scripts/index_code_graph.py /path/to/TargetApp

What it does:
  1. Clears all existing edges from code_graph_edges (full re-index)
  2. Parses every JS/TS file under the given path with tree-sitter
  3. Extracts call sites (caller → callee edges)
  4. Bulk-inserts all edges into Postgres
  5. Prints a summary: files, edges, top-called functions

Run this once after checkout, then again whenever the TargetApp codebase
changes significantly. Incremental updates (re-index only changed files) are
a follow-on feature.
"""
import sys
from pathlib import Path

# Allow running from the repo root without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.code_graph.graph import CodeGraph
from app.services.code_graph.store import clear_all_edges, edge_count, persist_edges
from app.services.database import init_db


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/index_code_graph.py <path-to-codebase>")
        print("Example: python scripts/index_code_graph.py /Users/Sidreddy/DevCode/SerpApiTestTool")
        sys.exit(1)

    codebase_path = sys.argv[1]
    if not Path(codebase_path).is_dir():
        print(f"Error: '{codebase_path}' is not a directory")
        sys.exit(1)

    # Ensure the code_graph_edges table exists
    print("Initialising database schema...")
    init_db()

    # Clear stale data before full re-index
    print("Clearing existing edges...")
    cleared = clear_all_edges()
    if cleared:
        print(f"  Removed {cleared} stale edges")

    # Parse the codebase and build the in-memory graph
    print(f"\nIndexing: {codebase_path}")
    print("  Parsing JS/TS files with tree-sitter...")
    graph = CodeGraph.build_from_directory(codebase_path)

    if not graph._edges:
        print("\nNo call edges found. Check that the path contains JS/TS source files.")
        sys.exit(0)

    # Persist to Postgres
    print(f"  Persisting {len(graph._edges)} edges to Postgres...")
    persisted = persist_edges(graph._edges)

    # Print summary
    stats = graph.stats()
    total_in_db = edge_count()

    print("\n" + "=" * 50)
    print("Call Graph Index — Summary")
    print("=" * 50)
    print(f"  Edges persisted:       {persisted}")
    print(f"  Total edges in DB:     {total_in_db}")
    print(f"  Unique callers:        {stats['unique_callers']}")
    print(f"  Unique callees:        {stats['unique_callees']}")

    if stats["top_called_functions"]:
        print("\n  Top called functions (most callers):")
        for name, count in stats["top_called_functions"]:
            print(f"    {name:40s}  {count} caller(s)")

    print("\nDone. The call graph is ready — restart the server to load it into memory.")


if __name__ == "__main__":
    main()
