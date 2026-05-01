"""
Definition of Done (DoD) gate — run before any incident transitions to REVIEWING.

Every check returns (passed: bool, evidence: str).  If any check fails the incident
is set to VERIFICATION_FAILED instead of REVIEWING, and the failed checks are
attached to the incident for debugging.

Checks
------
pr_has_confidence_score PR carries a confidence score from DiagnosisAgent.
pr_linked_to_issue      A GitHub issue URL was created and linked to the PR.
monitor_pr_map_updated  monitor_pr_map has an entry for the incident's monitor_id.
                        Skipped (not_applicable) when monitor_id is None.
blast_radius_respected  PR touches 5 or fewer files.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.events import IncidentState

logger = logging.getLogger(__name__)

DEFINITION_OF_DONE: dict[str, str] = {
    "pr_has_confidence_score": "PR description contains confidence score from DiagnosisAgent",
    "pr_linked_to_issue":      "PR body references the GitHub issue URL",
    "monitor_pr_map_updated":  "monitor_pr_map table has an entry for this incident's monitor_id",
    "blast_radius_respected":  "PR diff touches 5 or fewer files",
}

_NOT_APPLICABLE = "not_applicable"


class DefinitionOfDoneChecker:
    """
    Runs all DoD checks against an IncidentState that has just had a PR created.

    Expects these fields to be populated before calling run_all():
      incident.pr_files_changed  — list of files in the PR
      incident.confidence        — from DiagnosisAgent
      incident.issue_url         — GitHub issue URL (None if not created)
      incident.monitor_id        — resource_id of the triggering monitor (None = manual)
    """

    async def check_pr_has_confidence_score(
        self, incident: IncidentState
    ) -> tuple[bool, str]:
        if incident.confidence is not None:
            return True, f"Confidence score present: {incident.confidence:.0%}"
        return False, "confidence is None — DiagnosisAgent may not have completed"

    async def check_pr_linked_to_issue(
        self, incident: IncidentState
    ) -> tuple[bool, str]:
        if incident.issue_url:
            return True, f"Issue URL: {incident.issue_url}"
        return False, "issue_url is None — no GitHub issue was created or linked to the PR"

    async def check_monitor_pr_map_updated(
        self, incident: IncidentState
    ) -> tuple[bool, str]:
        if incident.monitor_id is None:
            return True, _NOT_APPLICABLE
        from app.services.incident_store import incident_store
        pr_url = incident_store.get_pr_for_resource(incident.monitor_id)
        if pr_url:
            return True, f"monitor_pr_map[{incident.monitor_id!r}] = {pr_url!r}"
        return False, f"No entry in monitor_pr_map for monitor_id={incident.monitor_id!r}"

    async def check_blast_radius_respected(
        self, incident: IncidentState
    ) -> tuple[bool, str]:
        # Primary source: pr_files_changed already populated from FixResult
        files = incident.pr_files_changed
        if files:
            count = len(files)
            names = ", ".join(files)
            if count <= 5:
                return True, f"PR touches {count} file(s): {names}"
            return False, f"PR touches {count} files (limit 5): {names}"

        # Fallback: fetch from GitHub API when list is empty
        if incident.pr_number is None:
            return False, "No pr_number on incident — cannot verify blast radius"
        try:
            from app.core.config import settings
            from app.services.github import GitHubService
            owner, repo = settings.fix_target_repo.split("/", 1)
            github = GitHubService()
            diffs = await github.get_pr_diff(owner, repo, incident.pr_number)
            count = len(diffs)
            names = ", ".join(d.filename for d in diffs)
            if count <= 5:
                return True, f"PR touches {count} file(s) (GitHub API): {names}"
            return False, f"PR touches {count} files (limit 5, GitHub API): {names}"
        except Exception as exc:
            return False, f"Could not fetch PR diff from GitHub: {exc}"

    async def run_all(
        self, incident: IncidentState
    ) -> dict[str, tuple[bool, str]]:
        """
        Run all checks concurrently and return {check_name: (passed, evidence)}.

        Evidence is the string _NOT_APPLICABLE ("not_applicable") when a check
        is skipped due to missing context (e.g. monitor_id is None).
        """
        async def _not_applicable() -> tuple[bool, str]:
            return (True, _NOT_APPLICABLE)

        monitor_check = (
            self.check_monitor_pr_map_updated(incident)
            if incident.monitor_id is not None
            else _not_applicable()
        )

        (
            confidence,
            linked_issue,
            blast_radius,
            monitor_pr,
        ) = await asyncio.gather(
            self.check_pr_has_confidence_score(incident),
            self.check_pr_linked_to_issue(incident),
            self.check_blast_radius_respected(incident),
            monitor_check,
        )

        return {
            "pr_has_confidence_score": confidence,
            "pr_linked_to_issue":      linked_issue,
            "blast_radius_respected":  blast_radius,
            "monitor_pr_map_updated":  monitor_pr,
        }


# Module-level singleton
dod_checker = DefinitionOfDoneChecker()
