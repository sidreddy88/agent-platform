"""
Protected-path profiles. The default (production) list blocks migrations/ and
auth/ by folder name; on SWE-bench that blocked 24 Django fixes to library
source (django/db/migrations/autodetector.py, django/contrib/auth/forms.py).
The "library" profile keeps real migration files, secrets, infra, lockfiles
and CI protected, without blocking library source by folder name.
"""
from __future__ import annotations

import pytest

from app.services.blast_radius import (
    DEFAULT_PROTECTED_PATTERNS,
    BlastRadiusGuard,
    protected_patterns_for,
)

LIBRARY_SOURCE = ["django/db/migrations/autodetector.py", "django/contrib/auth/forms.py",
                  "django/core/management/commands/makemigrations.py"]
ALWAYS_PROTECTED = ["django/contrib/auth/migrations/0001_initial.py", "migrations/0042_add_field.py",
                    ".env", "config/.env.production", ".github/workflows/ci.yml", "poetry.lock",
                    "infra/main.tf", "Dockerfile"]


def _blocked(path: str, profile: str) -> bool:
    return not BlastRadiusGuard(protected_patterns=protected_patterns_for(profile)).check([path]).allowed


@pytest.mark.parametrize("path", LIBRARY_SOURCE)
def test_default_profile_still_blocks_by_folder_name(path):
    assert _blocked(path, "default")


@pytest.mark.parametrize("path", LIBRARY_SOURCE)
def test_library_profile_allows_library_source(path):
    assert not _blocked(path, "library")


@pytest.mark.parametrize("path", ALWAYS_PROTECTED)
@pytest.mark.parametrize("profile", ["default", "library"])
def test_migration_files_secrets_infra_ci_stay_protected(path, profile):
    assert _blocked(path, profile)


def test_unset_or_unknown_profile_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("BLAST_RADIUS_PROFILE", raising=False)
    assert protected_patterns_for() == DEFAULT_PROTECTED_PATTERNS
    assert protected_patterns_for("bogus") == DEFAULT_PROTECTED_PATTERNS


def test_env_selects_the_profile(monkeypatch):
    monkeypatch.setenv("BLAST_RADIUS_PROFILE", "library")
    assert "**/auth/**" not in protected_patterns_for()
    monkeypatch.setenv("BLAST_RADIUS_PROFILE", "default")
    assert "**/auth/**" in protected_patterns_for()
