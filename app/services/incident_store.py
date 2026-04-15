"""
In-memory incident state store with JSON file persistence.

Incidents survive a server restart — written to .incidents.json on every update.
Replace with PostgreSQL (append-only event log) for production.
"""
import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

from app.models.events import ErrorEvent, IncidentState, IncidentStatus

logger = logging.getLogger(__name__)

PERSISTENCE_FILE = ".incidents.json"


class IncidentStore:
    def __init__(self):
        self._incidents: Dict[str, IncidentState] = {}
        # error_type / resource_id → pr_url — prevents duplicate PRs
        self._monitor_pr_map: Dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(self, error_event: ErrorEvent) -> IncidentState:
        incident = IncidentState(error_event=error_event, detected_at=datetime.utcnow())
        self._incidents[incident.id] = incident
        self._save()
        return incident

    def get(self, incident_id: str) -> Optional[IncidentState]:
        return self._incidents.get(incident_id)

    def update(self, incident: IncidentState) -> IncidentState:
        self._incidents[incident.id] = incident
        self._save()
        return incident

    def list_all(self) -> List[IncidentState]:
        return sorted(self._incidents.values(), key=lambda i: i.detected_at, reverse=True)

    def list_active(self) -> List[IncidentState]:
        terminal = {IncidentStatus.RESOLVED, IncidentStatus.NOISE, IncidentStatus.DUPLICATE}
        return [i for i in self.list_all() if i.status not in terminal]

    def get_open_pr_for_error(self, error_type: str, service: str) -> Optional[str]:
        """
        Return the PR URL if an open (non-resolved, non-rejected) incident for
        this exact error_type + service combo already has a PR.
        Used to prevent duplicate PRs for the same recurring error.
        """
        closed = {IncidentStatus.RESOLVED, IncidentStatus.REJECTED,
                  IncidentStatus.NOISE, IncidentStatus.DUPLICATE}
        for incident in self._incidents.values():
            if (
                incident.error_event.error_type == error_type
                and incident.error_event.service == service
                and incident.pr_url
                and incident.status not in closed
            ):
                return incident.pr_url
        return None

    def clear(self) -> int:
        """Delete all incidents from memory and disk. Returns count deleted."""
        count = len(self._incidents)
        self._incidents.clear()
        self._monitor_pr_map.clear()
        self._save()
        return count

    # ------------------------------------------------------------------
    # PR idempotency
    # ------------------------------------------------------------------

    def get_pr_for_resource(self, resource_id: str) -> Optional[str]:
        """Return existing PR url for a resource, or None."""
        return self._monitor_pr_map.get(resource_id)

    def set_pr_for_resource(self, resource_id: str, pr_url: str) -> None:
        self._monitor_pr_map[resource_id] = pr_url
        self._save()

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def metrics(self) -> dict:
        all_i = list(self._incidents.values())
        resolved = [i for i in all_i if i.status == IncidentStatus.RESOLVED]
        noise = [i for i in all_i if i.status == IncidentStatus.NOISE]
        duplicate = [i for i in all_i if i.status == IncidentStatus.DUPLICATE]
        mttr_values = [i.mttr_seconds for i in resolved if i.mttr_seconds is not None]
        return {
            "total": len(all_i),
            "active": len(self.list_active()),
            "resolved": len(resolved),
            "noise": len(noise),
            "duplicate": len(duplicate),
            "avg_mttr_seconds": round(sum(mttr_values) / len(mttr_values), 1) if mttr_values else None,
            "false_positive_rate": round(len(noise) / len(all_i), 3) if all_i else 0.0,
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self) -> None:
        try:
            data = {
                "incidents": [i.model_dump(mode="json") for i in self._incidents.values()],
                "monitor_pr_map": self._monitor_pr_map,
            }
            with open(PERSISTENCE_FILE, "w") as f:
                json.dump(data, f, default=str, indent=2)
        except Exception as exc:
            logger.warning("[IncidentStore] Could not persist to disk: %s", exc)

    def _load(self) -> None:
        if not os.path.exists(PERSISTENCE_FILE):
            return
        try:
            with open(PERSISTENCE_FILE) as f:
                data = json.load(f)
            for item in data.get("incidents", []):
                incident = IncidentState.model_validate(item)
                self._incidents[incident.id] = incident
            self._monitor_pr_map = data.get("monitor_pr_map", {})
            logger.info(
                "[IncidentStore] Loaded %d incidents from %s",
                len(self._incidents), PERSISTENCE_FILE,
            )
        except Exception as exc:
            logger.warning("[IncidentStore] Could not load from disk: %s", exc)


# Module-level singleton
incident_store = IncidentStore()
