"""
Tests for LocalRepoService — specifically the credential-prompt-suppression
and token-refresh protections on _pull().

Two real bugs found live, back to back:

1. _pull() didn't set GIT_TERMINAL_PROMPT=0 the way _clone() does. When the
   configured GITHUB_TOKEN was stale/invalid, `git pull` fell back to an
   interactive credential prompt instead of failing fast — which hung
   indefinitely in this headless context (no TTY to answer the prompt), and
   would have hung every diagnose() call silently in production, since
   ensure_fresh() runs at the start of every one.

2. GIT_TERMINAL_PROMPT=0 alone wasn't enough — recurred immediately after
   fixing #1. If GIT_ASKPASS is set in the environment (e.g. inherited from
   an editor's git integration), git delegates to that helper instead of
   respecting GIT_TERMINAL_PROMPT, and the helper hangs forever waiting on a
   GUI that doesn't exist headlessly. Fixed by also clearing GIT_ASKPASS.

A third, related gap (not a hang, but silent staleness): _pull() never
refreshed the origin remote URL, so a rotated GITHUB_TOKEN never reached an
already-cloned repo — it kept authenticating with the old, dead token baked
in at clone time. Fixed by running `git remote set-url` before every pull.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.repo import LocalRepoService


def _make_service(tmp_path) -> LocalRepoService:
    svc = LocalRepoService("owner", "repo")
    svc._path = tmp_path / "owner-repo"
    svc._path.mkdir()
    return svc


def _assert_protected(env: dict | None, cmd) -> None:
    env = env or {}
    assert env.get("GIT_TERMINAL_PROMPT") == "0", f"missing GIT_TERMINAL_PROMPT=0 on {cmd}"
    assert env.get("GIT_ASKPASS") == "", f"missing cleared GIT_ASKPASS on {cmd}"


class TestPullTerminalPromptProtection:
    @pytest.mark.asyncio
    async def test_pull_sets_git_terminal_prompt_zero_and_clears_askpass(self, tmp_path):
        svc = _make_service(tmp_path)

        with patch("app.services.repo._run", new=AsyncMock(return_value=(0, "", ""))) as mock_run:
            await svc._pull()

        # remote set-url (token refresh) + pull, both protected.
        assert mock_run.call_count == 2
        for call in mock_run.call_args_list:
            _, kwargs = call
            _assert_protected(kwargs.get("env"), call.args)

    @pytest.mark.asyncio
    async def test_pull_refreshes_remote_url_before_pulling(self, tmp_path):
        """A rotated GITHUB_TOKEN must reach an already-cloned repo -- the
        token is baked into the origin URL at clone time and _pull() must
        re-set it every time, or a revoked-and-rotated token keeps silently
        authenticating with the dead one."""
        svc = _make_service(tmp_path)

        call_log = []

        async def fake_run(cmd, env=None):
            call_log.append(cmd)
            return (0, "", "")

        with patch("app.services.repo._run", new=fake_run):
            await svc._pull()

        assert call_log[0][:4] == ["git", "-C", str(svc._path), "remote"]
        assert "set-url" in call_log[0]
        assert svc._clone_url() in call_log[0]
        assert call_log[1][:4] == ["git", "-C", str(svc._path), "pull"]

    @pytest.mark.asyncio
    async def test_pull_failure_fallback_also_protected(self, tmp_path):
        """The fetch + reset --hard fallback on pull failure must not
        regress back to an unprotected interactive prompt either."""
        svc = _make_service(tmp_path)

        call_log = []

        async def fake_run(cmd, env=None):
            call_log.append((cmd, env))
            if "pull" in cmd:
                return (1, "", "pull failed")
            if "rev-parse" in cmd:
                return (0, "main\n", "")
            return (0, "", "")

        with patch("app.services.repo._run", new=fake_run):
            await svc._pull()

        # The network-touching calls (set-url, pull, fetch) must carry the
        # protection. rev-parse (used by _current_branch) is local-only --
        # never touches the remote, so it's not part of this risk and isn't
        # asserted on.
        network_calls = [
            (cmd, env) for cmd, env in call_log if "pull" in cmd or "fetch" in cmd or "set-url" in cmd
        ]
        assert len(network_calls) == 3  # set-url, pull, fetch
        for cmd, env in network_calls:
            _assert_protected(env, cmd)

        # reset --hard is also local-only once fetch has updated refs, but
        # the fix passes env there too for consistency -- confirm it wasn't
        # dropped by accident.
        reset_calls = [(cmd, env) for cmd, env in call_log if "reset" in cmd]
        assert len(reset_calls) == 1
        _assert_protected(reset_calls[0][1], reset_calls[0][0])

    @pytest.mark.asyncio
    async def test_clone_still_sets_git_terminal_prompt_zero_and_clears_askpass(self, tmp_path):
        """Regression guard on the existing _clone() protection this fix mirrors."""
        svc = LocalRepoService("owner", "repo")
        svc._path = tmp_path / "owner-repo-clone"

        with patch("app.services.repo._run", new=AsyncMock(return_value=(0, "", ""))) as mock_run:
            await svc._clone()

        _, kwargs = mock_run.call_args
        _assert_protected(kwargs.get("env"), mock_run.call_args.args)
