"""
Regression tests for IncidentStore.forget_pr_mapping().

Real production bug: TriageAgent's duplicate-PR check (_check_duplicate_pr)
reads monitor_pr_map by a composite (error_type, service, description-prefix)
key, written once when a PR is first created (set_pr_for_resource) and never
updated again. Closing that PR later (it was never merged) doesn't touch this
map at all -- deleting the incident doesn't either, since it's a separate
table with no wiring back to incident deletion. Confirmed live: closed a PR,
deleted its incident, and the very next occurrence of the exact same crash was
still triaged "duplicate -- open PR already covers this" citing that same
closed PR, because the composite key (same error_type + service +
near-identical description prefix for a recurring crash) still resolved to
the stale URL.
"""
from __future__ import annotations

from app.services.incident_store import IncidentStore


def _store_with_map(mapping: dict[str, str]) -> IncidentStore:
    store = IncidentStore.__new__(IncidentStore)
    store._incidents = {}
    store._monitor_pr_map = dict(mapping)
    return store


def test_forget_pr_mapping_removes_matching_entries():
    store = _store_with_map({
        "APP_CRASHED:TaskTargetApp:CastError...": "https://github.com/org/repo/pull/2565",
        "/ecs/TaskTargetApp": "https://github.com/org/repo/pull/2565",
    })

    removed = store.forget_pr_mapping("https://github.com/org/repo/pull/2565")

    assert removed == 2
    assert store._monitor_pr_map == {}


def test_forget_pr_mapping_only_removes_the_matching_pr():
    """A stale PR being forgotten must not collateral-delete unrelated,
    still-valid mappings for a different PR."""
    store = _store_with_map({
        "APP_CRASHED:TaskTargetApp:CastError...": "https://github.com/org/repo/pull/2565",
        "S3_ERROR:ImageService:NoSuchKey...": "https://github.com/org/repo/pull/9999",
    })

    removed = store.forget_pr_mapping("https://github.com/org/repo/pull/2565")

    assert removed == 1
    assert store._monitor_pr_map == {
        "S3_ERROR:ImageService:NoSuchKey...": "https://github.com/org/repo/pull/9999",
    }


def test_forget_pr_mapping_returns_zero_when_nothing_matches():
    store = _store_with_map({"key": "https://github.com/org/repo/pull/1"})

    removed = store.forget_pr_mapping("https://github.com/org/repo/pull/2565")

    assert removed == 0
    assert store._monitor_pr_map == {"key": "https://github.com/org/repo/pull/1"}
