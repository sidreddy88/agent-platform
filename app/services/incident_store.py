"""
Incident state store backed by SQLite (agent_platform.db).

Incidents survive server restarts. State is loaded from the DB on startup
and written through on every mutation. A legacy .incidents.json file is
auto-migrated to the DB on first run and renamed to .incidents.json.migrated.
"""
import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

from app.models.events import ErrorEvent, IncidentState, IncidentStatus
from app.services.database import get_db

logger = logging.getLogger(__name__)

_LEGACY_JSON = ".incidents.json"


class IncidentStore:
    def __init__(self):
        self._incidents: Dict[str, IncidentState] = {}
        self._monitor_pr_map: Dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(self, error_event: ErrorEvent) -> IncidentState:
        incident = IncidentState(error_event=error_event, detected_at=datetime.utcnow())
        self._incidents[incident.id] = incident
        self._upsert_incident(incident)
        self._broadcast(incident)
        return incident

    def get(self, incident_id: str) -> Optional[IncidentState]:
        return self._incidents.get(incident_id)

    def update(self, incident: IncidentState) -> IncidentState:
        self._incidents[incident.id] = incident
        self._upsert_incident(incident)
        self._broadcast(incident)
        return incident

    def _broadcast(self, incident: IncidentState) -> None:
        try:
            import asyncio
            from app.api.websocket_dashboard import broadcast
            payload = {
                "type": "incident_update",
                "incident": {
                    **incident.model_dump(mode="json"),
                    "mttr_seconds": incident.mttr_seconds,
                    "age_seconds": incident.age_seconds,
                },
            }
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(broadcast(payload))
        except Exception:
            pass

    def list_all(self) -> List[IncidentState]:
        return sorted(self._incidents.values(), key=lambda i: i.detected_at, reverse=True)

    def list_active(self) -> List[IncidentState]:
        terminal = {IncidentStatus.RESOLVED, IncidentStatus.NOISE, IncidentStatus.DUPLICATE}
        return [i for i in self.list_all() if i.status not in terminal]

    def get_open_pr_for_error(self, error_type: str, service: str, description: str = "") -> Optional[str]:
        """Return PR URL if an open incident matches error_type + service + description prefix."""
        closed = {IncidentStatus.RESOLVED, IncidentStatus.REJECTED,
                  IncidentStatus.NOISE, IncidentStatus.DUPLICATE}
        desc_key = description[:100].strip()
        for incident in self._incidents.values():
            if incident.status in closed or not incident.pr_url:
                continue
            if (
                incident.error_event.error_type == error_type
                and incident.error_event.service == service
                and incident.error_event.description[:100].strip() == desc_key
            ):
                return incident.pr_url
        return None

    def get_resolved_for_error(self, error_type: str, service: str) -> Optional["IncidentState"]:
        """Return the most recently resolved incident for this error_type + service (regression check)."""
        candidates = [
            i for i in self._incidents.values()
            if i.status == IncidentStatus.RESOLVED
            and i.error_event.error_type == error_type
            and i.error_event.service == service
            and i.diagnosis
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda i: i.resolved_at or i.detected_at)

    def clear(self) -> int:
        """Delete all incidents. Returns count deleted."""
        count = len(self._incidents)
        self._incidents.clear()
        self._monitor_pr_map.clear()
        conn = get_db()
        try:
            conn.execute("DELETE FROM incidents")
            conn.execute("DELETE FROM monitor_pr_map")
            conn.commit()
        except Exception as exc:
            logger.warning("[IncidentStore] DB clear failed: %s", exc)
        finally:
            conn.close()
        return count

    # ------------------------------------------------------------------
    # PR idempotency
    # ------------------------------------------------------------------

    def get_pr_for_resource(self, resource_id: str) -> Optional[str]:
        return self._monitor_pr_map.get(resource_id)

    def set_pr_for_resource(self, resource_id: str, pr_url: str) -> None:
        self._monitor_pr_map[resource_id] = pr_url
        conn = get_db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO monitor_pr_map (resource_id, pr_url) VALUES (?, ?)",
                (resource_id, pr_url),
            )
            conn.commit()
        except Exception as exc:
            logger.warning("[IncidentStore] DB write (monitor_pr_map) failed: %s", exc)
        finally:
            conn.close()

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
    # DB helpers
    # ------------------------------------------------------------------

    def _upsert_incident(self, incident: IncidentState) -> None:
        conn = get_db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO incidents (id, status, detected_at, data) VALUES (?, ?, ?, ?)",
                (
                    incident.id,
                    incident.status.value,
                    incident.detected_at.isoformat(),
                    incident.model_dump_json(),
                ),
            )
            conn.commit()
        except Exception as exc:
            logger.warning("[IncidentStore] DB upsert failed: %s", exc)
        finally:
            conn.close()

    def _load(self) -> None:
        self._load_from_db()
        if not self._incidents:
            self._migrate_from_json()

    def _load_from_db(self) -> None:
        conn = get_db()
        try:
            for row in conn.execute("SELECT data FROM incidents"):
                incident = IncidentState.model_validate_json(row["data"])
                self._incidents[incident.id] = incident
            for row in conn.execute("SELECT resource_id, pr_url FROM monitor_pr_map"):
                self._monitor_pr_map[row["resource_id"]] = row["pr_url"]
            if self._incidents:
                logger.info("[IncidentStore] Loaded %d incidents from DB", len(self._incidents))
        except Exception as exc:
            logger.warning("[IncidentStore] DB load failed: %s", exc)
        finally:
            conn.close()

    def _migrate_from_json(self) -> None:
        if not os.path.exists(_LEGACY_JSON):
            return
        try:
            with open(_LEGACY_JSON) as f:
                data = json.load(f)
            for item in data.get("incidents", []):
                incident = IncidentState.model_validate(item)
                self._incidents[incident.id] = incident
                self._upsert_incident(incident)
            for resource_id, pr_url in data.get("monitor_pr_map", {}).items():
                self._monitor_pr_map[resource_id] = pr_url
                self.set_pr_for_resource(resource_id, pr_url)
            os.rename(_LEGACY_JSON, _LEGACY_JSON + ".migrated")
            logger.info(
                "[IncidentStore] Migrated %d incidents from %s → DB",
                len(self._incidents), _LEGACY_JSON,
            )
        except Exception as exc:
            logger.warning("[IncidentStore] JSON migration failed: %s", exc)


# Module-level singleton
incident_store = IncidentStore()
