import asyncio
import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

_TARGETS_DIR = Path(__file__).parent.parent.parent / "targets" / "target-app"

_CONFIG_INDEX_PATCH = (
    'assertInValues("NODE_ENV", ["production", "development", "local"])',
    'assertInValues("NODE_ENV", ["production", "development", "local", "test"])',
)

_SERVER_PATCH_OLD = (
    'app.listen(port, () => {\n'
    '  addIPToWhitelist().then(() => {}).catch(() => {});\n'
    '  console.log(`Server up and running on port ${port} !`)\n'
    '});'
)

_SERVER_PATCH_NEW = (
    'if (require.main === module) {\n'
    '  app.listen(port, () => {\n'
    '    addIPToWhitelist().then(() => {}).catch(() => {});\n'
    '    console.log(`Server up and running on port ${port} !`);\n'
    '  });\n'
    '}\n\n'
    'module.exports = app;'
)


@dataclass
class SandboxResult:
    passed: bool
    output: str
    error: str | None = None


class SandboxService:
    """
    Validates a fix by running the TargetApp test suite in an isolated Docker container.

    Lifecycle per run:
      1. Clone TargetApp to a temp dir
      2. Apply fix files
      3. Apply harness patches (config/index.js, server.js, package.json)
      4. Copy test infrastructure from targets/target-app/
      5. docker compose up --build  (runs npm test inside the container)
      6. Capture pass/fail + output
      7. docker compose down -v  (destroy everything)
      8. Delete temp dir
    """

    async def run(self, fix_files: dict[str, str], incident_id: str) -> SandboxResult:
        """Run the test suite with fix_files applied. Always cleans up, even on error."""
        tmp = tempfile.mkdtemp(prefix=f"target-app-{incident_id[:8]}-")
        try:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._run_sync, fix_files, tmp
            )
        except Exception as exc:
            logger.error("[Sandbox] Unexpected error: %s", exc)
            return SandboxResult(passed=False, output="", error=str(exc))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            logger.info("[Sandbox] Cleaned up temp dir %s", tmp)

    def _run_sync(self, fix_files: dict[str, str], workdir: str) -> SandboxResult:
        logger.info("[Sandbox] Starting in %s", workdir)

        # 1. Clone
        clone = subprocess.run(
            [
                "git", "clone", "--depth=1",
                f"https://{settings.github_token}@github.com/TargetOrg/TargetApp.git",
                workdir,
            ],
            capture_output=True, text=True,
        )
        if clone.returncode != 0:
            return SandboxResult(passed=False, output="", error=f"Clone failed: {clone.stderr.strip()}")
        logger.info("[Sandbox] Cloned TargetApp")

        # 2. Apply fix files
        for rel_path, content in fix_files.items():
            dest = Path(workdir) / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
        logger.info("[Sandbox] Applied %d fix file(s)", len(fix_files))

        # 3. Apply harness patches
        error = self._apply_patches(workdir)
        if error:
            return SandboxResult(passed=False, output="", error=error)
        logger.info("[Sandbox] Patches applied")

        # 4. Copy test infrastructure
        self._copy_test_infra(workdir)
        logger.info("[Sandbox] Test infrastructure copied")

        # 5–7. Docker run + cleanup
        return self._run_docker(workdir)

    def _apply_patches(self, workdir: str) -> str | None:
        # config/index.js — add "test" to valid NODE_ENV values
        config_path = Path(workdir) / "config" / "index.js"
        try:
            text = config_path.read_text()
            config_path.write_text(text.replace(*_CONFIG_INDEX_PATCH))
        except FileNotFoundError:
            return "config/index.js not found in cloned repo"

        # server.js — export app + guard app.listen()
        server_path = Path(workdir) / "server.js"
        try:
            text = server_path.read_text()
            patched = text.replace(_SERVER_PATCH_OLD, _SERVER_PATCH_NEW)
            if patched == text:
                logger.warning("[Sandbox] server.js patch did not match — server may already be patched")
            server_path.write_text(patched)
        except FileNotFoundError:
            return "server.js not found in cloned repo"

        # package.json — add devDependencies + scripts
        pkg_path = Path(workdir) / "package.json"
        try:
            pkg = json.loads(pkg_path.read_text())
            overrides = json.loads((_TARGETS_DIR / "package-overrides.json").read_text())
            pkg.setdefault("scripts", {}).update(overrides["scripts_to_add"])
            pkg.setdefault("devDependencies", {}).update(overrides["devDependencies_to_add"])
            pkg_path.write_text(json.dumps(pkg, indent=2))
        except FileNotFoundError:
            return "package.json not found in cloned repo"

        return None

    def _copy_test_infra(self, workdir: str) -> None:
        for item in ("tests", "jest.config.js", "config/config.test.json"):
            src = _TARGETS_DIR / item
            dst = Path(workdir) / item
            if not src.exists():
                logger.warning("[Sandbox] Test infra item missing: %s", src)
                continue
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

        for fname in ("Dockerfile.test", "docker-compose.test.yml"):
            shutil.copy2(_TARGETS_DIR / fname, Path(workdir) / fname)

    def _docker_available(self) -> bool:
        try:
            result = subprocess.run(
                ["docker", "info"], capture_output=True, timeout=5
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _run_docker(self, workdir: str) -> SandboxResult:
        if not self._docker_available():
            logger.warning("[Sandbox] Docker daemon not available — falling back to direct npm test")
            return self._run_npm_direct(workdir)

        # Derive a Docker-safe project name (only lowercase alphanumeric + hyphens)
        import re as _re
        project_name = _re.sub(r"[^a-z0-9-]", "", Path(workdir).name.lower())[:40] or "target-app"
        compose_cmd = ["docker", "compose", "-f", "docker-compose.test.yml", "-p", project_name]
        try:
            result = subprocess.run(
                compose_cmd + [
                    "up", "--build",
                    "--abort-on-container-exit",
                    "--exit-code-from", "app",
                ],
                capture_output=True, text=True, cwd=workdir, timeout=300,
            )
            output = result.stdout + result.stderr
            passed = result.returncode == 0
            logger.info("[Sandbox] Tests %s via Docker (exit %d)", "PASSED" if passed else "FAILED", result.returncode)
            return SandboxResult(passed=passed, output=output)
        except subprocess.TimeoutExpired:
            logger.error("[Sandbox] Docker timed out after 5 minutes")
            return SandboxResult(passed=False, output="", error="Sandbox timed out after 5 minutes")
        finally:
            subprocess.run(
                compose_cmd + ["down", "-v", "--remove-orphans"],
                capture_output=True, cwd=workdir,
            )
            logger.info("[Sandbox] Docker compose torn down")

    def _run_npm_direct(self, workdir: str) -> SandboxResult:
        """Run npm install + npm test directly without Docker."""
        import os
        env = {**os.environ, "NODE_ENV": "test"}

        install = subprocess.run(
            ["npm", "install"],
            capture_output=True, text=True, cwd=workdir, timeout=180, env=env,
        )
        if install.returncode != 0:
            logger.error("[Sandbox] npm install failed")
            return SandboxResult(passed=False, output=install.stderr, error="npm install failed")

        result = subprocess.run(
            ["npm", "test"],
            capture_output=True, text=True, cwd=workdir, timeout=180, env=env,
        )
        output = result.stdout + result.stderr
        passed = result.returncode == 0
        logger.info("[Sandbox] Tests %s via direct npm test (exit %d)", "PASSED" if passed else "FAILED", result.returncode)
        return SandboxResult(passed=passed, output=output)
