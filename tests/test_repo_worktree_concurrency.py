"""Concurrent pinned worktrees off one base clone (harness optimizer lanes)."""
import asyncio
import subprocess
from pathlib import Path

import pytest

from app.services import repo as repo_mod
from app.services.repo import LocalRepoService


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def origin(tmp_path, monkeypatch):
    """A local 'origin' with 3 commits, and a base clone of it, standing in for GitHub."""
    src = tmp_path / "origin"
    src.mkdir()
    _git("init", "-q", cwd=src)
    _git("config", "user.email", "t@t", cwd=src)
    _git("config", "user.name", "t", cwd=src)
    _git("config", "uploadpack.allowReachableSHA1InWant", "true", cwd=src)
    shas = []
    for i in range(3):
        (src / "f.py").write_text(f"v = {i}\n")
        _git("add", ".", cwd=src)
        _git("commit", "-qm", f"c{i}", cwd=src)
        shas.append(subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, check=True,
                                   capture_output=True, text=True).stdout.strip())
    root = tmp_path / "clones"
    monkeypatch.setattr(repo_mod.settings, "repo_clone_root", str(root), raising=False)
    monkeypatch.setattr(LocalRepoService, "_clone_url", lambda self: f"file://{src}")
    return shas


def test_concurrent_pinned_worktrees_of_one_repo_are_isolated(origin):
    async def one(sha):
        r = LocalRepoService("o", "r", pinned_sha=sha)
        await r.ensure_fresh()
        content = r.read_file("f.py")
        await asyncio.sleep(0.05)       # others add/prune meanwhile
        still_there = r.file_exists("f.py")
        await r.remove_worktree()
        return content, still_there, r.local_path

    async def main():
        # Every sha twice: two trials of one case must not share a worktree.
        return await asyncio.gather(*(one(s) for s in origin + origin))

    results = asyncio.run(main())
    assert [c for c, _, _ in results] == [f"v = {i}\n" for i in (0, 1, 2)] * 2
    assert all(ok for _, ok, _ in results)
    assert len({p for _, _, p in results}) == 6
    assert not any(Path(p).exists() for _, _, p in results)


def test_files_containing_is_a_superset_of_python_substring_matches(origin, tmp_path):
    """The git-grep prefilter must never drop a file Python's own substring
    scan would match: tracked files, untracked files and symlinks."""
    import os

    async def go():
        r = LocalRepoService("o", "r", pinned_sha=origin[-1])
        await r.ensure_fresh()
        root = r.local_path
        (root / "untracked.py").write_text("needle here\n")
        os.symlink("f.py", root / "link.py")
        found = r.files_containing("needle")
        python = {p for p in r.list_files() if "needle" in (root / p).read_text(errors="ignore")}
        await r.remove_worktree()
        return found, python

    found, python = asyncio.run(go())
    assert found is not None and python <= found
    assert "untracked.py" in found and "link.py" in found
    assert "f.py" not in found          # tracked, doesn't contain the text: skipped


def test_candidate_files_falls_back_to_a_full_scan_without_a_prefilter():
    from unittest.mock import MagicMock

    from app.agents.diagnosis import _candidate_files
    repo = MagicMock()
    repo.list_files.return_value = {"b.py", "a.py"}
    repo.files_containing.return_value = None
    assert _candidate_files(repo, "x") == ["a.py", "b.py"]
    repo.files_containing.return_value = {"b.py", ".git/config"}
    assert _candidate_files(repo, "x") == ["b.py"]
    mock_only = MagicMock()                     # tests' MagicMock repos: no real prefilter
    mock_only.list_files.return_value = {"z.py"}
    assert _candidate_files(mock_only, "x") == ["z.py"]


def test_two_processes_share_a_base_clone_without_git_lock_collisions(origin, tmp_path):
    """The failure seen live: two processes fetching into one shallow clone at
    once hit git's shallow.lock. The fcntl lock beside the clone serializes
    them across processes."""
    import subprocess
    import sys
    from app.services import repo as repo_mod
    root = repo_mod.settings.repo_clone_root
    src = tmp_path / "origin"
    script = f"""
import asyncio, sys
sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
from app.services import repo as repo_mod
from app.services.repo import LocalRepoService
repo_mod.settings.repo_clone_root = {root!r}
LocalRepoService._clone_url = lambda self: "file://{src}"
async def main():
    async def one(sha):
        r = LocalRepoService("o", "r", pinned_sha=sha)
        await r.ensure_fresh()
        ok = r.file_exists("f.py")
        await r.remove_worktree()
        return ok
    print(all(await asyncio.gather(*(one(s) for s in {origin!r} * 3))))
asyncio.run(main())
"""
    procs = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(3)]
    outs = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode == 0 for p in procs), [e[-400:] for _, e in outs]
    assert all(o.strip() == "True" for o, _ in outs)
