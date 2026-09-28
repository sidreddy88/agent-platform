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
