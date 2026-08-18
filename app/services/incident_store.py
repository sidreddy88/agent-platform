"""
Incident state store — write-through to Postgres (SQLite in local dev).

Incidents survive server restarts. State is loaded from the DB on startup
and written through on every mutation.

Dedup queries (get_open_pr_for_error, get_resolved_for_error) go directly
to the database so they are correct across multiple ECS tasks sharing the
same Postgres instance. Other reads use the in-memory dict for speed.

A legacy .incidents.json file is auto-migrated to the DB on first run.
"""
import json
import logging
import os
import re
from datetime import datetime
from typing import Dict, List, Optional

from app.models.events import ErrorEvent, IncidentState, IncidentStatus
# NOTE: legacy get_db() reference removed — call sites use the SQLAlchemy
# `engine` + `tables` API (or the `upsert` helper) directly.

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
        # Carry the event's resource_id as monitor_id so the DoD gate can check
        # monitor_pr_map coverage. None for manually-triggered incidents.
        incident.monitor_id = error_event.resource_id
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

    @staticmethod
    def _normalize_desc(description: str) -> str:
        """Normalize variable tokens (numbers, hashes, IDs) so similar errors match."""
        collapsed = re.sub(r'\s+', ' ', description[:300]).strip()
        return re.sub(r'\b[a-f0-9]{8,}\b|\b\d+[a-zA-Z]*\b', 'X', collapsed[:100])

    def get_open_pr_for_error(self, error_type: str, service: str, description: str = "") -> Optional[str]:
        """Query Postgres for non-terminal incidents matching error_type + service +
        normalized description. Direct DB query ensures correctness across ECS tasks.
        FIX_FAILED / VERIFICATION_FAILED are skipped — a new attempt is allowed."""
        from sqlalchemy import select
        from app.services.database import engine, tables

        skip_statuses = {
            IncidentStatus.REJECTED.value, IncidentStatus.NOISE.value,
            IncidentStatus.DUPLICATE.value, IncidentStatus.FIX_FAILED.value,
            IncidentStatus.VERIFICATION_FAILED.value,
        }
        desc_key = self._normalize_desc(description)

        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    select(tables.incidents.c.data)
                    .where(tables.incidents.c.status.not_in(list(skip_statuses)))
                ).fetchall()
            candidates = [IncidentState.model_validate_json(row.data) for row in rows]
        except Exception as exc:
            logger.warning("[IncidentStore] get_open_pr_for_error DB query failed, using cache: %s", exc)
            candidates = list(self._incidents.values())

        for incident in candidates:
            if incident.status == IncidentStatus.RESOLVED and incident.outcome != "fix_merged":
                continue
            if (
                incident.error_event.error_type == error_type
                and incident.error_event.service == service
                and self._normalize_desc(incident.error_event.description or "") == desc_key
            ):
                return incident.pr_url or incident.id
        return None

    def get_resolved_for_error(
        self, error_type: str, service: str, description: str = ""
    ) -> Optional["IncidentState"]:
        """Query Postgres for resolved incidents matching error_type + service + a
        normalized description (same _normalize_desc used by get_open_pr_for_error).

        Real production bug: matching on error_type + service ALONE is far too
        coarse for a service that crashes for many unrelated reasons under the
        same generic "APP_CRASHED" type. Confirmed live: a previewCode CastError
        incident's "regression check" surfaced an unrelated image-upload crash
        (different route, different file, different root cause entirely) as "the
        same error, previously resolved" -- purely because both happened to be
        APP_CRASHED on TaskTargetApp. DiagnosisAgent's prompt frames this
        result as "PRIOR KNOWLEDGE -- treat as strong evidence," with no
        verification step at all (unlike blast_radius/additional_fix_targets,
        which are snippet-grounded). The model then produced a confident,
        specific-sounding root_cause narrative -- citing the real (but wrong)
        incident ID and PR as evidence that 7 sibling files were already fixed --
        blending the wrong citation with AGENTS.md's own illustrative "7 siblings"
        anecdote into something that read as verified history but wasn't.

        Requiring a normalized-description match (not just error_type + service)
        makes this return None far more often for a genuinely-new-but-similarly-
        typed crash -- the safe failure mode. A missed regression just means
        diagnosis reasons from scratch, same as if no history existed at all. A
        false-positive regression match poisons diagnosis with "strong evidence"
        that's actually wrong, which is far worse. If description is omitted,
        no result is ever returned (matches get_open_pr_for_error's same
        empty-description behavior) rather than falling back to the old
        error_type+service-only matching.
        """
        from sqlalchemy import select
        from app.services.database import engine, tables

        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    select(tables.incidents.c.data)
                    .where(tables.incidents.c.status == IncidentStatus.RESOLVED.value)
                ).fetchall()
            all_resolved = [IncidentState.model_validate_json(row.data) for row in rows]
        except Exception as exc:
            logger.warning("[IncidentStore] get_resolved_for_error DB query failed, using cache: %s", exc)
            all_resolved = [i for i in self._incidents.values() if i.status == IncidentStatus.RESOLVED]

        desc_key = self._normalize_desc(description)
        candidates = [
            i for i in all_resolved
            if i.error_event.error_type == error_type
            and i.error_event.service == service
            and i.diagnosis
            and self._normalize_desc(i.error_event.description or "") == desc_key
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda i: i.resolved_at or i.detected_at)

    def delete(self, incident_id: str) -> None:
        self._incidents.pop(incident_id, None)
        from app.services.database import engine, tables
        try:
            with engine.begin() as conn:
                conn.execute(
                    tables.incidents.delete().where(tables.incidents.c.id == incident_id)
                )
        except Exception as exc:
            logger.warning("[IncidentStore] DB delete failed: %s", exc)

    def clear(self) -> int:
        """Delete all non-resolved incidents. Resolved incidents are preserved. Returns count deleted."""
        to_delete = [
            i for i in self._incidents.values()
            if i.status != IncidentStatus.RESOLVED
        ]
        if not to_delete:
            return 0
        for incident in to_delete:
            del self._incidents[incident.id]
        from app.services.database import engine, tables
        try:
            ids = [i.id for i in to_delete]
            with engine.begin() as conn:
                conn.execute(
                    tables.incidents.delete().where(tables.incidents.c.id.in_(ids))
                )
        except Exception as exc:
            logger.warning("[IncidentStore] DB clear failed: %s", exc)
        return len(to_delete)

    # ------------------------------------------------------------------
    # PR idempotency
    # ------------------------------------------------------------------

    def get_pr_for_resource(self, resource_id: str) -> Optional[str]:
        return self._monitor_pr_map.get(resource_id)

    def set_pr_for_resource(self, resource_id: str, pr_url: str) -> None:
        self._monitor_pr_map[resource_id] = pr_url
        from app.services.database import tables, upsert
        try:
            upsert(tables.monitor_pr_map, {"resource_id": resource_id, "pr_url": pr_url})
        except Exception as exc:
            logger.warning("[IncidentStore] DB write (monitor_pr_map) failed: %s", exc)

    def forget_pr_mapping(self, pr_url: str) -> int:
        """Remove every monitor_pr_map entry pointing at `pr_url`.

        Real production bug: TriageAgent.get_occurrence_count's duplicate check
        (_check_duplicate_pr in triage.py) reads this map by (error_type, service,
        description-prefix) key and, separately, by monitor_id -- but nothing ever
        writes to it EXCEPT set_pr_for_resource, called once when a PR is first
        created. Closing that PR later (it was never merged, e.g. because
        FixGenerationAgent's fix was wrong or incomplete) doesn't touch this map at
        all. Confirmed live: a PR was closed, its incident deleted, and the very
        next occurrence of the exact same crash was still triaged "duplicate --
        open PR already covers this" citing that same closed PR, because the
        composite key (same error_type + service + near-identical description
        prefix for a recurring crash) still resolved to the stale URL. Deleting the
        incident alone doesn't fix this -- the map is a separate table, keyed
        differently, with no wiring back to incident deletion until this method.

        Returns the number of entries removed.
        """
        stale_keys = [k for k, v in self._monitor_pr_map.items() if v == pr_url]
        for k in stale_keys:
            del self._monitor_pr_map[k]
        if stale_keys:
            from sqlalchemy import delete as sa_delete
            from app.services.database import engine, tables
            try:
                with engine.begin() as conn:
                    conn.execute(
                        sa_delete(tables.monitor_pr_map).where(tables.monitor_pr_map.c.pr_url == pr_url)
                    )
            except Exception as exc:
                logger.warning("[IncidentStore] DB delete (monitor_pr_map) failed: %s", exc)
        return len(stale_keys)

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
        from app.services.database import tables, upsert
        try:
            upsert(tables.incidents, {
                "id": incident.id,
                "status": incident.status.value,
                "detected_at": incident.detected_at.isoformat(),
                "data": incident.model_dump_json(),
            })
        except Exception as exc:
            logger.warning("[IncidentStore] DB upsert failed: %s", exc)

    def _load(self) -> None:
        self._load_from_db()
        if not self._incidents:
            self._migrate_from_json()

    def _load_from_db(self) -> None:
        from sqlalchemy import select
        from app.services.database import engine, tables
        try:
            with engine.connect() as conn:
                for row in conn.execute(select(tables.incidents.c.data)).all():
                    incident = IncidentState.model_validate_json(row.data)
                    self._incidents[incident.id] = incident
                for row in conn.execute(select(tables.monitor_pr_map)).all():
                    self._monitor_pr_map[row.resource_id] = row.pr_url
            if self._incidents:
                logger.info("[IncidentStore] Loaded %d incidents from DB", len(self._incidents))
        except Exception as exc:
            logger.warning("[IncidentStore] DB load failed: %s", exc)

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
