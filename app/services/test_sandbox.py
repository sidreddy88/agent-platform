"""
TestSandbox — a container for running a case's code and tests during offline evals.

Each SWE-bench case gets the same prebuilt image the official harness grades in
(swebench/sweb.eval.x86_64.<id>), on Modal, with the network blocked. Test mode
(app/services/repo_tests.py, the fix harness's test_mode setting) runs the
repo's existing tests here before and after a fix. The hidden grading tests are
not in the image (the harness adds them at grading time), and nothing written
here reaches the patch: the patch is the diff of the files the agent edits.

Production does not use this; its fix agent validates in app/services/sandbox.py.
`modal` is imported lazily so the app never needs it.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shlex
import time
from typing import Protocol

logger = logging.getLogger(__name__)

# Output the model sees per command: the start (usually the command's own
# errors) and the end (pytest's summary), with the middle dropped.
_HEAD_CHARS = 1500
_TAIL_CHARS = 3500
DEFAULT_COMMAND_TIMEOUT = 180
MAX_COMMAND_TIMEOUT = 600


def trim_output(text: str, head: int = _HEAD_CHARS, tail: int = _TAIL_CHARS) -> str:
    if len(text) <= head + tail:
        return text
    dropped = len(text) - head - tail
    return f"{text[:head]}\n... [{dropped} characters omitted] ...\n{text[-tail:]}"


# Commands that look for the answer instead of working it out: the repo's git
# history, other installed copies or caches, whole-filesystem searches. A smoke
# run (2026-10-08) tried all of these; the network block can't stop local ones.
_ANSWER_HUNTING = re.compile(
    r"\bgit\s+(log|show|reflog|fsck|cat-file|rev-list|blame|whatchanged|grep|stash|checkout\s+\S+\s+--)\b"
    r"|--all\b|\.cache\b|/pkgs/|site-packages/(?!.*testbed)"
    r"|\bfind\s+/(\s|$)|\bgrep\b[^|;&]*\s/(\s|$)|\b(locate|updatedb)\b"
)


def refused_command(command: str) -> str | None:
    """Why a command isn't allowed, or None if it is."""
    if _ANSWER_HUNTING.search(command):
        return ("REFUSED: this command searches git history, caches or other copies of the code. Work "
                "from the repository as it is: read the code, reproduce the bug, fix it, and test it.")
    return None


def swebench_image(instance_id: str) -> str:
    """The official SWE-bench x86_64 eval image for an instance."""
    return f"docker.io/swebench/sweb.eval.x86_64.{instance_id.lower().replace('__', '_1776_')}:latest"


class TestSandbox(Protocol):
    async def write_file(self, path: str, content: str) -> None: ...
    async def run(self, command: str, timeout: int = DEFAULT_COMMAND_TIMEOUT,
                  trim: bool = True) -> tuple[int, str]: ...
    async def close(self) -> None: ...


class ModalTestSandbox:
    """One SWE-bench case's container on Modal. Repo at /testbed, conda env "testbed"."""

    APP_NAME = "agent-platform-test-loop"
    WORKDIR = "/testbed"

    def __init__(self, image: str, lifetime: int = 60 * 45, cpu: float = 2.0) -> None:
        self._image_name = image
        self._lifetime = lifetime
        self._cpu = cpu
        self._sb = None

    async def start(self) -> "ModalTestSandbox":
        import modal

        def _create():
            app = modal.App.lookup(self.APP_NAME, create_if_missing=True)
            image = modal.Image.from_registry(self._image_name)
            return modal.Sandbox.create(app=app, image=image, timeout=self._lifetime,
                                        cpu=self._cpu, block_network=True, workdir=self.WORKDIR)

        t0 = time.monotonic()
        self._sb = await asyncio.to_thread(_create)
        # Caches can hold other versions of the code under test; the agent must
        # work from the repo as it is (a smoke run went looking in the pip cache).
        await self.run("rm -rf /root/.cache/pip ~/.cache/pip /opt/miniconda3/pkgs/*/ 2>/dev/null; true", timeout=60)
        logger.info("[TestSandbox] started %s in %.1fs", self._image_name, time.monotonic() - t0)
        return self

    async def write_file(self, path: str, content: str) -> None:
        target = path if path.startswith("/") else f"{self.WORKDIR}/{path}"

        def _write():
            import base64
            # Modal's filesystem API first; the exec fallback writes base64 in chunks,
            # because one exec's arguments are capped at 64 KB (a single-argument write
            # failed on astropy/units/quantity.py, 112 KB as base64).
            try:
                self._sb.filesystem.write_text(content, target)
                return
            except Exception as exc:
                logger.debug("[TestSandbox] filesystem.write_text failed (%r), using exec", exc)
            b64 = base64.b64encode(content.encode()).decode()
            q = shlex.quote(target)
            p = self._sb.exec("bash", "-c", f"mkdir -p \"$(dirname {q})\" && : > {q}.b64")
            p.wait()
            for i in range(0, len(b64), 48_000):
                p = self._sb.exec("bash", "-c", f"printf %s {b64[i:i + 48_000]} >> {q}.b64")
                p.wait()
            p = self._sb.exec("bash", "-c", f"base64 -d {q}.b64 > {q} && rm {q}.b64")
            p.wait()
            if p.returncode != 0:
                raise RuntimeError(f"write_file {target} failed: {p.stderr.read()[:300]}")

        await asyncio.to_thread(_write)

    async def run(self, command: str, timeout: int = DEFAULT_COMMAND_TIMEOUT,
                  trim: bool = True) -> tuple[int, str]:
        """Exit code and output. trim=False keeps all of it (test results are parsed from it)."""
        timeout = max(1, min(int(timeout or DEFAULT_COMMAND_TIMEOUT), MAX_COMMAND_TIMEOUT))
        # Same environment the eval script uses: conda's testbed env, in the repo.
        wrapped = ("source /opt/miniconda3/bin/activate >/dev/null 2>&1; conda activate testbed >/dev/null 2>&1; "
                   f"cd {self.WORKDIR} && ( {command} ) 2>&1")

        def _run():
            p = self._sb.exec("bash", "-c", f"timeout {timeout} bash -c {shlex.quote(wrapped)}",
                              timeout=timeout + 30)
            out = p.stdout.read()
            p.wait()
            return p.returncode, out

        try:
            code, out = await asyncio.to_thread(_run)
        except Exception as exc:  # the exec itself failed (sandbox gone, Modal error)
            return -1, f"[sandbox error: {exc}]"
        if code == 124:
            out += f"\n[command timed out after {timeout}s]"
        return code, trim_output(out) if trim else out

    async def close(self) -> None:
        if self._sb is not None:
            try:
                await asyncio.to_thread(self._sb.terminate)
            except Exception as exc:
                logger.warning("[TestSandbox] terminate failed: %s", exc)
            self._sb = None
