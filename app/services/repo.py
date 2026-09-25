"""
LocalRepoService — maintains a persistent shallow clone of a GitHub repo.

Reads (file existence, content) come from the local clone.
Writes (create branch, update file, create PR) stay on the GitHub API.

Usage:
    repo = LocalRepoService("your-org", "your-repo")
    await repo.ensure_fresh()          # clone or pull
    exists = repo.file_exists("routes/api/image.js")
    content = repo.read_file("routes/api/image.js")
    all_paths = repo.list_files()      # set[str] of all blob paths
"""

import asyncio
import logging
import os
import shutil
import signal
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

_DEFAULT_CLONE_ROOT = os.path.join(
    os.path.expanduser("~"), ".agent-platform", "repos"
)


class LocalRepoService:
    def __init__(
        self,
        owner: str,
        repo: str,
        branch: str | None = None,
        pinned_sha: str | None = None,
    ) -> None:
        self._owner = owner
        self._repo = repo
        self._branch = branch  # None → detect default branch on first clone
        self._pinned_sha = pinned_sha
        clone_root = getattr(settings, "repo_clone_root", None) or _DEFAULT_CLONE_ROOT
        self._base_path = Path(clone_root) / f"{owner}-{repo}"
        if pinned_sha:
            # Isolated worktree off the shared base clone, keyed by SHA. Used
            # by replay/eval tooling (scripts/eval_diagnosis_regression.py) that
            # needs the repo as it existed *before* a specific fix landed —
            # the live shared clone (self._base_path, unpinned instances)
            # always tracks current HEAD, which by definition no longer has
            # the bug for any already-merged ground-truth case.
            self._path = Path(clone_root) / "_eval_worktrees" / f"{owner}-{repo}-{pinned_sha[:12]}"
        else:
            self._path = self._base_path
        self._ready = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def ensure_fresh(self) -> None:
        """Clone if not present, pull to latest otherwise.

        When constructed with pinned_sha, checks out an isolated worktree at
        that exact commit instead — see the pinned_sha comment in __init__.
        """
        if self._pinned_sha:
            await self._ensure_pinned_worktree()
        elif self._is_cloned():
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

    @property
    def pinned(self) -> bool:
        """True if this instance is checked out at a fixed historical SHA
        rather than tracking live HEAD — see pinned_sha in __init__."""
        return bool(self._pinned_sha)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _is_cloned(self) -> bool:
        return (self._path / ".git").exists()

    def _clone_url(self) -> str:
        token = settings.github_token
        if token:
            # x-access-token as username + token as password — git sends both
            # in the HTTP Basic header without prompting, works in non-TTY envs.
            return f"https://x-access-token:{token}@github.com/{self._owner}/{self._repo}.git"
        return f"https://github.com/{self._owner}/{self._repo}.git"

    async def _clone(self, target: Path | None = None) -> None:
        target = target or self._path
        target.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "clone", "--depth=1"]
        if self._branch:
            cmd += ["--branch", self._branch]
        cmd += [self._clone_url(), str(target)]
        logger.info("LocalRepo: cloning %s/%s → %s", self._owner, self._repo, target)
        # Disable interactive credential prompts — fail fast in non-TTY envs.
        # GIT_ASKPASS must be cleared too, not just GIT_TERMINAL_PROMPT — see
        # the comment in _pull() for why (GIT_TERMINAL_PROMPT alone doesn't
        # stop git from delegating to an inherited GIT_ASKPASS helper).
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}
        rc, _, stderr = await _run(cmd, env=env)
        if rc != 0:
            raise RuntimeError(f"git clone failed: {stderr.strip()}")
        logger.info("LocalRepo: clone complete (%s/%s)", self._owner, self._repo)

    async def _ensure_pinned_worktree(self) -> None:
        """Check out self._pinned_sha into an isolated worktree at self._path.

        Reuses the shared base clone (self._base_path) as the object store —
        cloning fresh per case would be needlessly slow and this repo's
        history is small enough that fetching one extra commit is cheap.
        NOT safe against a concurrent live diagnose() call mutating the same
        base clone (e.g. _pull()'s reset --hard) — fine for single-operator
        manual eval runs, not for concurrent multi-writer use.
        """
        if self._path.exists():
            return  # already checked out (idempotent — reruns reuse it)
        if not (self._base_path / ".git").exists():
            await self._clone(target=self._base_path)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}
        await _run(
            ["git", "-C", str(self._base_path), "remote", "set-url", "origin", self._clone_url()],
            env=env,
        )
        rc, _, stderr = await _run(
            ["git", "-C", str(self._base_path), "fetch", "--depth=1", "origin", self._pinned_sha],
            env=env,
        )
        if rc != 0:
            raise RuntimeError(f"git fetch of pinned sha {self._pinned_sha} failed: {stderr.strip()}")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        rc, _, stderr = await _run(
            ["git", "-C", str(self._base_path), "worktree", "add", "--detach", str(self._path), self._pinned_sha],
            env=env,
        )
        if rc != 0:
            raise RuntimeError(f"git worktree add failed for {self._pinned_sha}: {stderr.strip()}")
        logger.info("LocalRepo: pinned worktree ready at %s (%s)", self._path, self._pinned_sha)

    async def remove_worktree(self) -> None:
        """Tear down a pinned worktree. No-op for a normal (unpinned) instance.

        Deletes the directory directly and then runs `git worktree prune`,
        instead of `git worktree remove --force`. In CI, `worktree remove`
        for sympy__sympy-13091 never returned, in two separate gate runs:
        once for 93 min until the job was killed, and once past a 120s
        timeout where even the post-kill wait blocked (repo.py _run ->
        asyncio subprocess wait), meaning something still held git's output
        pipes. It never reproduced locally (2.5s). rmtree + prune does the
        same cleanup without that command.

        Best-effort: this runs in `finally` blocks, so it must not raise and
        replace the exception that is actually propagating.
        """
        if not self._pinned_sha or not self._path.exists():
            return
        try:
            await asyncio.to_thread(shutil.rmtree, self._path, ignore_errors=True)
        except Exception as exc:
            logger.warning("LocalRepo: could not delete worktree %s: %s", self._path, exc)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}
        rc, _, stderr = await _run(
            ["git", "-C", str(self._base_path), "worktree", "prune"], env=env, timeout=120,
        )
        if rc != 0:
            logger.warning("LocalRepo: worktree prune failed for %s: %s", self._base_path, stderr.strip())
        self._ready = False

    async def _pull(self) -> None:
        logger.info("LocalRepo: pulling %s/%s", self._owner, self._repo)
        # Same GIT_TERMINAL_PROMPT=0 protection as _clone() -- without it, an
        # auth failure here (rotated/expired token, revoked access) makes git
        # fall back to an interactive credential prompt instead of failing
        # fast. Found live: a stale GITHUB_TOKEN caused this to hang
        # indefinitely instead of failing in under a second, which would have
        # hung every diagnose() call silently (ensure_fresh() runs at the
        # start of every one) instead of surfacing a clear auth error.
        #
        # GIT_TERMINAL_PROMPT=0 alone is NOT enough: it only suppresses git's
        # own tty prompt. If GIT_ASKPASS is set in the environment (e.g. an
        # editor's git integration exports it globally, as VS Code's does),
        # git delegates to that helper instead -- which then hangs forever
        # waiting on a GUI that doesn't exist in a headless/background
        # context. Found live, immediately after the first fix landed: the
        # exact same infinite hang recurred with GIT_TERMINAL_PROMPT=0
        # already in place, because GIT_ASKPASS was inherited from the
        # spawning shell. Clearing GIT_ASKPASS makes git respect
        # GIT_TERMINAL_PROMPT again and fail fast instead.
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}
        # Also refresh the remote URL to the current GITHUB_TOKEN. Without
        # this, a rotated token in settings never reaches an already-cloned
        # repo -- the token is baked into the origin URL at clone time and
        # _pull() never touches it again, so a revoked-and-rotated token
        # keeps silently authenticating with the dead one until someone
        # deletes the local clone by hand. Found live: this repo had been
        # cloned under the old token; rotating GITHUB_TOKEN in .env had no
        # effect until this.
        await _run(["git", "-C", str(self._path), "remote", "set-url", "origin", self._clone_url()], env=env)
        rc, _, stderr = await _run(["git", "-C", str(self._path), "pull", "--ff-only"], env=env)
        if rc != 0:
            logger.warning("LocalRepo: pull failed (%s) — resetting to origin", stderr.strip())
            branch = await self._current_branch()
            await _run(["git", "-C", str(self._path), "fetch", "origin"], env=env)
            await _run(["git", "-C", str(self._path), "reset", "--hard", f"origin/{branch}"], env=env)

    async def _current_branch(self) -> str:
        _, stdout, _ = await _run(
            ["git", "-C", str(self._path), "rev-parse", "--abbrev-ref", "HEAD"]
        )
        return stdout.strip() or "HEAD"


# Upper bound on any single git subprocess. Generous on purpose -- a first
# clone of a large repo (sympy, django) takes minutes -- but finite: with no
# timeout at all, one stuck git call (lock contention, a network stall, an
# unexpected prompt) blocks diagnose() forever. A diagnosis gate shard sat
# silent for 93 minutes until the job timeout killed it; a git call in the
# replay's cleanup path is one of the unbounded waits it could have been in.
GIT_TIMEOUT_SECONDS = 900
_POST_KILL_WAIT_SECONDS = 10


async def _run(cmd: list[str], env: dict | None = None,
               timeout: float = GIT_TIMEOUT_SECONDS) -> tuple[int, str, str]:
    """Run a subprocess; on timeout, kill it and return rc=-1 with the reason
    in stderr, so callers' existing `rc != 0` handling reports it."""
    # Own process group, so a timeout kills git *and* anything it spawned.
    # Killing only git was not enough: in CI the post-kill wait still blocked,
    # because asyncio's Process.wait() doesn't return until the stdout/stderr
    # pipes close, and a surviving child was holding them.
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            # Bounded: a child that left the process group (setsid) can still
            # hold the pipes; abandon the wait rather than block on it.
            await asyncio.wait_for(proc.wait(), _POST_KILL_WAIT_SECONDS)
        except asyncio.TimeoutError:
            logger.error("LocalRepo: %s still held its pipes after kill -- abandoning", cmd[:4])
        logger.error("LocalRepo: %s timed out after %.0fs -- killed", " ".join(cmd[:4]), timeout)
        return -1, "", f"timed out after {timeout:.0f}s: {' '.join(cmd)}"
    return proc.returncode, stdout.decode(), stderr.decode()
