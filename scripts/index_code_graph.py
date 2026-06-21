"""
CLI — bulk index the target codebase into the call graph.

Usage:
    # Use a local path:
    python scripts/index_code_graph.py /path/to/TargetApp

    # Clone from GitHub (uses GITHUB_TOKEN env var):
    python scripts/index_code_graph.py --repo TargetOrg/TargetApp

    # No args — falls back to settings.codebase_path, then settings.fix_target_repo:
    python scripts/index_code_graph.py

What it does:
  1. Resolves the codebase path (local dir, GitHub clone, or config default)
  2. Clears all existing edges from code_graph_edges (full re-index)
  3. Parses every JS/TS file under the path with tree-sitter
  4. Extracts call sites (caller → callee edges)
  5. Bulk-inserts all edges into Postgres
  6. Prints a summary: files, edges, top-called functions
  7. Removes the temp clone if one was created
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Allow running from the repo root without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.config import settings
from app.services.code_graph.graph import CodeGraph
from app.services.code_graph.store import clear_all_edges, edge_count, persist_edges
from app.services.database import init_db


def clone_repo(repo: str) -> str:
    """Clone a GitHub repo into a temp directory and return the path."""
    token = settings.github_token or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("Error: GITHUB_TOKEN not set — required to clone from GitHub")
        sys.exit(1)

    tmp = tempfile.mkdtemp(prefix="code_graph_clone_")
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    print(f"Cloning {repo} into temp directory...")
    result = subprocess.run(
        ["git", "clone", "--depth=1", url, tmp],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"Error: git clone failed\n{result.stderr}")
        sys.exit(1)
    print(f"  Cloned to {tmp}")
    return tmp


def resolve_codebase(args: list[str]) -> tuple[str, bool]:
    """Return (codebase_path, cloned) — cloned=True means caller must clean up."""
    if len(args) >= 2 and args[1] == "--repo":
        if len(args) < 3:
            print("Usage: python scripts/index_code_graph.py --repo owner/repo")
            sys.exit(1)
        return clone_repo(args[2]), True

    if len(args) >= 2:
        return args[1], False

    # No args — use config defaults
    local_path = settings.codebase_path
    if local_path and Path(local_path).is_dir():
        print(f"Using codebase_path from config: {local_path}")
        return local_path, False

    # Fall back to cloning fix_target_repo
    repo = settings.fix_target_repo
    if repo:
        print(f"codebase_path not available locally — cloning {repo} from GitHub")
        return clone_repo(repo), True

    print("Error: no codebase path available. Pass a path, --repo owner/repo, or set CODEBASE_PATH in env.")
    sys.exit(1)


def main() -> None:
    codebase_path, cloned = resolve_codebase(sys.argv)

    if not Path(codebase_path).is_dir():
        print(f"Error: '{codebase_path}' is not a directory")
        sys.exit(1)

    try:
        print("Initialising database schema...")
        init_db()

        print("Clearing existing edges...")
        cleared = clear_all_edges()
        if cleared:
            print(f"  Removed {cleared} stale edges")

        print(f"\nIndexing: {codebase_path}")
        print("  Parsing JS/TS files with tree-sitter...")
        graph = CodeGraph.build_from_directory(codebase_path)

        if not graph._edges:
            print("\nNo call edges found. Check that the path contains JS/TS source files.")
            sys.exit(0)

        print(f"  Persisting {len(graph._edges)} edges to Postgres...")
        persisted = persist_edges(graph._edges)

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

    finally:
        if cloned:
            print(f"\nCleaning up temp clone: {codebase_path}")
            shutil.rmtree(codebase_path, ignore_errors=True)


if __name__ == "__main__":
    main()
