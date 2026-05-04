"""
BlastRadiusGuard — gates AI-generated PRs against configurable safety limits.

Checks (in order):
  1. Protected paths  — never touch migrations/, auth/, secrets, lockfiles, etc.
  2. Max files        — PR must not touch more than N files
  3. Max lines added  — net additions in the fix must not exceed the limit
  4. Max lines deleted — net deletions must not exceed the limit

Usage:
    guard = BlastRadiusGuard()
    result = guard.check(
        files=["routes/services/image.js", "tests/services/image.test.js"],
        additions=35,
        deletions=12,
    )
    if not result.allowed:
        # escalate to human
        print(result.reason)

All limits are configurable at construction time; defaults are conservative.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Sequence

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_MAX_FILES = 5
DEFAULT_MAX_LINES_ADDED = 500
DEFAULT_MAX_LINES_DELETED = 300

# Paths the AI must never touch — matched against each file with fnmatch.
# Patterns are checked against the raw path AND a leading-slash-stripped version.
DEFAULT_PROTECTED_PATTERNS: list[str] = [
    # Database migrations — schema changes must always be human-authored
    "migrations/**",
    "**/migrations/**",
    "**/*migration*",
    "db/migrate/**",
    "**/db/migrate/**",

    # Auth / security — never AI-modified
    "auth/**",
    "**/auth/**",
    "**/authentication/**",
    "**/authorization/**",
    "**/security/**",

    # Secrets and credentials — match both root-level and nested paths
    ".env*",          # root-level: .env, .env.production, .env.local
    "**/.env*",       # nested: config/.env.production
    "**/secrets/**",
    "**/credentials*",
    "**/*secret*",
    "**/*.pem",
    "**/*.key",
    "**/*.cert",
    "**/*.p12",
    "**/*.pfx",

    # Config files that affect infra / deployment
    "**/database.yml",
    "**/database.yaml",
    "config/database*",
    "**/terraform/**",
    "**/*.tf",
    "**/*.tfvars",
    "**/*.tfstate",
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "Dockerfile",
    "**/Dockerfile*",

    # Dependency lockfiles — high churn, hard to review meaningfully
    "package-lock.json",
    "yarn.lock",
    "poetry.lock",
    "Pipfile.lock",
    "Gemfile.lock",
    "go.sum",
    "Cargo.lock",

    # CI/CD pipelines — changes here can break the whole deployment process
    ".github/workflows/**",
    ".gitlab-ci.yml",
    "Jenkinsfile",
    ".circleci/**",
]


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class BlastRadiusResult:
    allowed: bool
    violations: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        """One-line summary of all violations, or 'OK'."""
        return "; ".join(self.violations) if self.violations else "OK"


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------

class BlastRadiusGuard:
    """
    Validates a proposed set of file changes against configurable safety limits.

    All thresholds can be overridden at construction time so tests can use
    tighter or looser values without touching defaults.
    """

    def __init__(
        self,
        max_files: int = DEFAULT_MAX_FILES,
        max_lines_added: int = DEFAULT_MAX_LINES_ADDED,
        max_lines_deleted: int = DEFAULT_MAX_LINES_DELETED,
        protected_patterns: Sequence[str] = DEFAULT_PROTECTED_PATTERNS,
    ) -> None:
        self.max_files = max_files
        self.max_lines_added = max_lines_added
        self.max_lines_deleted = max_lines_deleted
        self.protected_patterns = list(protected_patterns)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(
        self,
        files: Sequence[str],
        additions: int = 0,
        deletions: int = 0,
    ) -> BlastRadiusResult:
        """
        Run all blast radius checks and return a result.

        Args:
            files:     list of file paths the PR will touch
            additions: number of lines being added (net)
            deletions: number of lines being deleted (net)
        """
        violations: list[str] = []

        # 1. Protected paths
        for path in files:
            hit = self._protected_match(path)
            if hit:
                violations.append(f"Protected path '{path}' matches pattern '{hit}'")

        # 2. File count
        if len(files) > self.max_files:
            violations.append(
                f"Too many files: {len(files)} > limit {self.max_files}"
            )

        # 3. Lines added
        if additions > self.max_lines_added:
            violations.append(
                f"Too many additions: {additions} lines > limit {self.max_lines_added}"
            )

        # 4. Lines deleted
        if deletions > self.max_lines_deleted:
            violations.append(
                f"Too many deletions: {deletions} lines > limit {self.max_lines_deleted}"
            )

        return BlastRadiusResult(allowed=len(violations) == 0, violations=violations)

    def is_protected(self, path: str) -> bool:
        """Return True if the path matches any protected pattern."""
        return self._protected_match(path) is not None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _protected_match(self, path: str) -> str | None:
        """
        Return the first matching protected pattern, or None.
        Checks both the raw path and a leading-slash-stripped version.
        """
        stripped = path.lstrip("/")
        for pattern in self.protected_patterns:
            if fnmatch(path, pattern) or fnmatch(stripped, pattern):
                return pattern
        return None


# ---------------------------------------------------------------------------
# Module-level singleton (default limits)
# ---------------------------------------------------------------------------

blast_radius_guard = BlastRadiusGuard()
