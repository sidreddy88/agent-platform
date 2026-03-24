"""
In-memory incident state store.
Replace with PostgreSQL (append-only event log) for production.
"""
from datetime import datetime
from typing import Dict, List, Optional
from app.models.events import ErrorEvent, IncidentState, IncidentStatus


class IncidentStore:
    def __init__(self):
        self._incidents: Dict[str, IncidentState] = {}
        # resource_id → pr_url — prevents duplicate PRs for same resource
        self._monitor_pr_map: Dict[str, str] = {}

    def create(self, error_event: ErrorEvent) -> IncidentState:
        incident = IncidentState(error_event=error_event, detected_at=datetime.utcnow())
        self._incidents[incident.id] = incident
        return incident

    def get(self, incident_id: str) -> Optional[IncidentState]:
        return self._incidents.get(incident_id)

    def update(self, incident: IncidentState) -> IncidentState:
        self._incidents[incident.id] = incident
        return incident

    def list_all(self) -> List[IncidentState]:
        return sorted(self._incidents.values(), key=lambda i: i.detected_at, reverse=True)

    def list_active(self) -> List[IncidentState]:
        terminal = {IncidentStatus.RESOLVED, IncidentStatus.NOISE, IncidentStatus.DUPLICATE}
        return [i for i in self.list_all() if i.status not in terminal]

    def get_pr_for_resource(self, resource_id: str) -> Optional[str]:
        """Duplicate prevention — return existing PR url for a resource if one exists."""
        return self._monitor_pr_map.get(resource_id)

    def set_pr_for_resource(self, resource_id: str, pr_url: str) -> None:
        self._monitor_pr_map[resource_id] = pr_url

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


# Module-level singleton
incident_store = IncidentStore()
