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


atlas_service = MongoDBAtlasService()
