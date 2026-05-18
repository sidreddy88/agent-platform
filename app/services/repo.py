"""
LocalRepoService — maintains a persistent shallow clone of a GitHub repo.

Reads (file existence, content) come from the local clone.
Writes (create branch, update file, create PR) stay on the GitHub API.

Usage:
    repo = LocalRepoService("TargetOrg", "TargetApp")
    await repo.ensure_fresh()          # clone or pull
    exists = repo.file_exists("routes/api/image.js")
    content = repo.read_file("routes/api/image.js")
    all_paths = repo.list_files()      # set[str] of all blob paths
"""

import asyncio
import logging
import os
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

_DEFAULT_CLONE_ROOT = os.path.join(
    os.path.expanduser("~"), ".agent-platform", "repos"
)


class LocalRepoService:
    def __init__(self, owner: str, repo: str, branch: str | None = None) -> None:
        self._owner = owner
        self._repo = repo
        self._branch = branch  # None → detect default branch on first clone
        clone_root = getattr(settings, "repo_clone_root", None) or _DEFAULT_CLONE_ROOT
        self._path = Path(clone_root) / f"{owner}-{repo}"
        self._ready = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def ensure_fresh(self) -> None:
        """Clone if not present, pull to latest otherwise."""
        if self._is_cloned():
            await self._pull()
        else:
            await self._clone()
        self._ready = True

    def file_exists(self, path: str) -> bool:
        return (self._path / path.lstrip("/")).exists()

    def read_file(self, path: str) -> str:
        return (self._path / path.lstrip("/")).read_text(encoding="utf-8")

    def list_files(self) -> set[str]:
        """Return relative paths of all non-.git files."""
        result: set[str] = set()
        for p in self._path.rglob("*"):
            if p.is_file() and ".git" not in p.parts:
                result.add(str(p.relative_to(self._path)))
        return result

    @property
    def local_path(self) -> Path:
        return self._path

    @property
    def ready(self) -> bool:
        return self._ready

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _is_cloned(self) -> bool:
        return (self._path / ".git").exists()

    def _clone_url(self) -> str:
        token = settings.github_token
        if token:
            return f"https://{token}@github.com/{self._owner}/{self._repo}.git"
        return f"https://github.com/{self._owner}/{self._repo}.git"

    async def _clone(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "clone", "--depth=1"]
        if self._branch:
            cmd += ["--branch", self._branch]
        cmd += [self._clone_url(), str(self._path)]
        logger.info("LocalRepo: cloning %s/%s → %s", self._owner, self._repo, self._path)
        rc, _, stderr = await _run(cmd)
        if rc != 0:
            raise RuntimeError(f"git clone failed: {stderr.strip()}")
        logger.info("LocalRepo: clone complete (%s/%s)", self._owner, self._repo)

    async def _pull(self) -> None:
        logger.info("LocalRepo: pulling %s/%s", self._owner, self._repo)
        rc, _, stderr = await _run(["git", "-C", str(self._path), "pull", "--ff-only"])
        if rc != 0:
            logger.warning("LocalRepo: pull failed (%s) — resetting to origin", stderr.strip())
            branch = await self._current_branch()
            await _run(["git", "-C", str(self._path), "fetch", "origin"])
            await _run(["git", "-C", str(self._path), "reset", "--hard", f"origin/{branch}"])

    async def _current_branch(self) -> str:
        _, stdout, _ = await _run(
            ["git", "-C", str(self._path), "rev-parse", "--abbrev-ref", "HEAD"]
        )
        return stdout.strip() or "HEAD"


async def _run(cmd: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(), stderr.decode()
