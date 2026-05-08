"""
MongoDB Atlas monitoring service — uses the Atlas Administration API v2.

Checks per cluster:
  - Cluster state (IDLE / CREATING / UPDATING / DELETING / REPAIRING)
  - Current connections
  - Disk usage %
  - Ops/sec (insert + query + update + delete)
  - Replication lag (seconds, primaries excluded)

Auth: HTTP Digest with Atlas public/private API key pair.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

BASE = "https://cloud.mongodb.com/api/atlas/v2"
HEADERS = {"Accept": "application/vnd.atlas.2023-02-01+json"}


@dataclass
class AtlasClusterMetrics:
    name: str
    state: str                          # IDLE | CREATING | UPDATING | REPAIRING | DELETING
    mongo_version: str
    connections: Optional[int]
    disk_used_pct: Optional[float]      # 0–100
    ops_per_sec: Optional[float]        # total insert+query+update+delete
    replication_lag_sec: Optional[float]
    healthy: bool


@dataclass
class SlowQuery:
    namespace: str          # "<db>.<collection>"
    query_shape: str        # redacted shape Atlas reports
    exec_count: int
    avg_ms: float
    total_ms: float
    latest_at: str          # ISO timestamp of latest occurrence


@dataclass
class SuggestedIndex:
    namespace: str
    index_def: str          # human-readable index spec, e.g. "{ userId: 1, status: 1 }"
    impact: list[str]       # query shapes this index would help
    weight: float           # Atlas's score for the index (higher = more impact)


class MongoDBAtlasService:
    def __init__(self) -> None:
        self._public_key: str = getattr(settings, "atlas_public_key", "")
        self._private_key: str = getattr(settings, "atlas_private_key", "")
        self._project_id: str = getattr(settings, "atlas_project_id", "")

    @property
    def _configured(self) -> bool:
        return bool(self._public_key and self._private_key and self._project_id)

    def _auth(self) -> httpx.DigestAuth:
        return httpx.DigestAuth(self._public_key, self._private_key)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{BASE}{path}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, auth=self._auth(), headers=HEADERS, params=params)
            resp.raise_for_status()
            return resp.json()

    async def _latest_value(self, measurements: list[dict]) -> Optional[float]:
        """Return the most recent non-None data point from a measurement series."""
        for m in measurements:
            for dp in reversed(m.get("dataPoints", [])):
                if dp.get("value") is not None:
                    return float(dp["value"])
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_clusters(self) -> list[AtlasClusterMetrics]:
        if not self._configured:
            return []

        try:
            data = await self._get(f"/groups/{self._project_id}/clusters")
        except Exception as exc:
            logger.warning("Atlas: failed to list clusters: %s", exc)
            return []

        clusters = data.get("results", [])
        results = []
        for c in clusters:
            name = c.get("name", "unknown")
            state = c.get("stateName", "UNKNOWN")
            version = c.get("mongoDBVersion", "")

            connections, disk_pct, ops, lag = await self._get_cluster_metrics(name)

            healthy = state == "IDLE"
            results.append(AtlasClusterMetrics(
                name=name,
                state=state,
                mongo_version=version,
                connections=connections,
                disk_used_pct=disk_pct,
                ops_per_sec=ops,
                replication_lag_sec=lag,
                healthy=healthy,
            ))

        return results

    async def _get_cluster_metrics(
        self, cluster_name: str
    ) -> tuple[Optional[int], Optional[float], Optional[float], Optional[float]]:
        """Fetch connections, disk %, ops/sec, replication lag for a cluster."""
        try:
            proc_data = await self._get(
                f"/groups/{self._project_id}/processes",
                params={"clusterNames": cluster_name},
            )
        except Exception as exc:
            logger.warning("Atlas: failed to get processes for %s: %s", cluster_name, exc)
            return None, None, None, None

        processes = proc_data.get("results", [])
        if not processes:
            return None, None, None, None

        # Pick the PRIMARY (or first process if no primary found)
        primary = next((p for p in processes if p.get("typeName") == "REPLICA_PRIMARY"), processes[0])
        host_id = primary.get("id", "")  # format: hostname:port

        connections = await self._fetch_metric(host_id, ["CONNECTIONS"], "PT1M", "PT2H")
        disk_used = await self._fetch_metric(host_id, ["DISK_PARTITION_SPACE_USED_DATA"], "PT1M", "PT2H")
        disk_free = await self._fetch_metric(host_id, ["DISK_PARTITION_SPACE_FREE_DATA"], "PT1M", "PT2H")

        op_metrics = [
            "OPCOUNTER_INSERT", "OPCOUNTER_QUERY",
            "OPCOUNTER_UPDATE", "OPCOUNTER_DELETE",
        ]
        ops_values = []
        for metric in op_metrics:
            v = await self._fetch_metric(host_id, [metric], "PT1M", "PT2H")
            if v is not None:
                ops_values.append(v)

        # Replication lag — only meaningful on secondaries
        secondaries = [p for p in processes if p.get("typeName") == "REPLICA_SECONDARY"]
        lag: Optional[float] = None
        if secondaries:
            lag = await self._fetch_metric(secondaries[0].get("id", ""), ["REPLICATION_LAG"], "PT1M", "PT2H")

        # Disk % from used + free
        disk_pct: Optional[float] = None
        if disk_used is not None and disk_free is not None and (disk_used + disk_free) > 0:
            disk_pct = round(disk_used / (disk_used + disk_free) * 100, 1)

        ops_total = round(sum(ops_values), 2) if ops_values else None
        conn_int = int(connections) if connections is not None else None

        return conn_int, disk_pct, ops_total, lag

    async def _fetch_metric(
        self,
        process_id: str,
        metric_names: list[str],
        granularity: str,
        period: str,
    ) -> Optional[float]:
        if not process_id:
            return None
        try:
            data = await self._get(
                f"/groups/{self._project_id}/processes/{process_id}/measurements",
                params={"granularity": granularity, "period": period, "m": metric_names},
            )
            return await self._latest_value(data.get("measurements", []))
        except Exception as exc:
            logger.debug("Atlas: metric fetch failed (%s) for %s: %s", metric_names, process_id, exc)
            return None

    # ------------------------------------------------------------------
    # Performance Advisor — slow queries + suggested indexes
    # ------------------------------------------------------------------

    async def _primary_host_ids(self) -> list[str]:
        """Return all process IDs (host:port) Atlas knows about, primaries first.

        Performance Advisor data is collected per-process; we ask the primary
        of each cluster (Atlas surfaces the same advisor data on all members,
        but the primary is canonical).
        """
        if not self._configured:
            return []
        try:
            data = await self._get(f"/groups/{self._project_id}/processes")
        except Exception as exc:
            logger.warning("Atlas: failed to list processes: %s", exc)
            return []

        processes = data.get("results", []) or []
        # Order: primaries first, then secondaries, then any unclassified.
        primaries = [p["id"] for p in processes if p.get("typeName") == "REPLICA_PRIMARY" and p.get("id")]
        if primaries:
            return primaries
        # Fallback: any process. Performance Advisor still works on
        # secondaries — the data is replica-set wide.
        return [p["id"] for p in processes if p.get("id")]

    async def get_slow_queries(self, host_id: str, hours: int = 24) -> list[SlowQuery]:
        """Atlas slow-query log for a process. Empty list if not configured or call fails."""
        if not self._configured or not host_id:
            return []
        try:
            data = await self._get(
                f"/groups/{self._project_id}/processes/{host_id}/performanceAdvisor/slowQueryLogs",
                params={"duration": f"PT{int(hours)}H"},
            )
        except Exception as exc:
            logger.warning("Atlas: slowQueryLogs failed for %s: %s", host_id, exc)
            return []

        out: list[SlowQuery] = []
        for entry in data.get("slowQueries", []) or []:
            stats = entry.get("metrics") or {}
            out.append(SlowQuery(
                namespace=entry.get("namespace", "unknown"),
                query_shape=str(entry.get("line") or entry.get("queryShape", "")),
                exec_count=int(stats.get("execCount", 0) or 0),
                avg_ms=float(stats.get("execTimeMillis", 0) or 0),
                total_ms=float(stats.get("totalTimeMillis", 0) or 0),
                latest_at=str(entry.get("opTime", "")),
            ))
        return out

    async def get_suggested_indexes(self, host_id: str, hours: int = 24) -> list[SuggestedIndex]:
        """Atlas's suggested indexes for a process. Empty list on failure."""
        if not self._configured or not host_id:
            return []
        try:
            data = await self._get(
                f"/groups/{self._project_id}/processes/{host_id}/performanceAdvisor/suggestedIndexes",
                params={"duration": f"PT{int(hours)}H"},
            )
        except Exception as exc:
            logger.warning("Atlas: suggestedIndexes failed for %s: %s", host_id, exc)
            return []

        out: list[SuggestedIndex] = []
        for entry in data.get("suggestedIndexes", []) or []:
            keys = entry.get("index") or []
            # Atlas returns index keys as a list of {<field>: 1|-1} dicts;
            # render as "{ field: 1, field2: -1 }" for display.
            parts = []
            for k in keys:
                if isinstance(k, dict):
                    for field, direction in k.items():
                        parts.append(f"{field}: {direction}")
            index_def = "{ " + ", ".join(parts) + " }" if parts else str(keys)

            impact_shapes: list[str] = []
            for imp in entry.get("impact") or []:
                if isinstance(imp, dict):
                    shape = imp.get("queryShape") or imp.get("shape") or ""
                    if shape:
                        impact_shapes.append(str(shape))

            out.append(SuggestedIndex(
                namespace=entry.get("namespace", "unknown"),
                index_def=index_def,
                impact=impact_shapes,
                weight=float(entry.get("weight", 0) or 0),
            ))
        return out

    async def get_performance_advisor(
        self, hours: int = 24,
    ) -> tuple[list[SlowQuery], list[SuggestedIndex]]:
        """Fan out across all primary processes; aggregate slow queries + suggested indexes.

        Dedupes by (namespace, query_shape) for slow queries and
        (namespace, index_def) for suggested indexes — Atlas reports the
        same patterns on every replica-set member.
        """
        host_ids = await self._primary_host_ids()
        if not host_ids:
            return [], []

        seen_q: dict[tuple[str, str], SlowQuery] = {}
        seen_i: dict[tuple[str, str], SuggestedIndex] = {}

        for host_id in host_ids:
            for q in await self.get_slow_queries(host_id, hours=hours):
                key = (q.namespace, q.query_shape)
                # Keep the row with the higher total_ms — that's the worst observation.
                prev = seen_q.get(key)
                if prev is None or q.total_ms > prev.total_ms:
                    seen_q[key] = q
            for ix in await self.get_suggested_indexes(host_id, hours=hours):
                key = (ix.namespace, ix.index_def)
                prev = seen_i.get(key)
                if prev is None or ix.weight > prev.weight:
                    seen_i[key] = ix

        slow = sorted(seen_q.values(), key=lambda q: q.total_ms, reverse=True)
        idx = sorted(seen_i.values(), key=lambda i: i.weight, reverse=True)
        return slow, idx


atlas_service = MongoDBAtlasService()
