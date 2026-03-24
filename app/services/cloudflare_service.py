"""
Cloudflare service — zone analytics (requests, error rate, cache hit rate) via GraphQL API.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import httpx

from app.core.config import settings


@dataclass
class ZoneMetrics:
    zone_id: str
    zone_name: str
    total_requests: int
    cached_requests: int
    total_bytes: int
    cached_bytes: int
    http_2xx: int
    http_3xx: int
    http_4xx: int
    http_5xx: int
    cache_hit_rate: float   # 0.0 – 1.0
    error_rate: float       # 5xx / total
    threats_blocked: int


@dataclass
class CloudflareStatus:
    zones: List[ZoneMetrics] = field(default_factory=list)
    total_requests: int = 0
    total_5xx: int = 0
    overall_error_rate: float = 0.0
    overall_cache_hit_rate: float = 0.0


_ANALYTICS_QUERY = """
{
  viewer {
    zones(filter: {zoneTag: "%s"}) {
      httpRequests1mGroups(
        limit: %d
        filter: {datetime_geq: "%s", datetime_leq: "%s"}
      ) {
        sum {
          requests
          cachedRequests
          bytes
          cachedBytes
          threats
          responseStatusMap { edgeResponseStatus requests }
        }
      }
    }
  }
}
"""


class CloudflareService:
    BASE_URL = "https://api.cloudflare.com/client/v4"

    def __init__(self):
        self.token: str = getattr(settings, "cloudflare_api_token", "")
        raw_zones: str = getattr(settings, "cloudflare_zone_ids", "")
        self.zone_ids: List[str] = [z.strip() for z in raw_zones.split(",") if z.strip()]

    @property
    def _configured(self) -> bool:
        return bool(self.token and self.zone_ids)

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def _get_zone_name(self, zone_id: str) -> str:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self.BASE_URL}/zones/{zone_id}",
                    headers=self._headers,
                    timeout=10,
                )
                if resp.status_code == 200:
                    return resp.json().get("result", {}).get("name", zone_id)
        except Exception:
            pass
        return zone_id

    async def get_zone_analytics(
        self, zone_id: str, since_minutes: int = 30
    ) -> Optional[ZoneMetrics]:
        now = datetime.now(timezone.utc)
        since = (now - timedelta(minutes=since_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        until = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        # Use enough groups to cover the window (1 group per minute)
        limit = max(since_minutes, 1)
        query = _ANALYTICS_QUERY % (zone_id, limit, since, until)

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://api.cloudflare.com/client/v4/graphql",
                    headers=self._headers,
                    json={"query": query},
                    timeout=20,
                )
                if resp.status_code != 200:
                    return None
                data = resp.json()

            zones = data.get("data", {}).get("viewer", {}).get("zones", [])
            if not zones:
                return None

            groups = zones[0].get("httpRequests1mGroups", [])
            zone_name = await self._get_zone_name(zone_id)

            if not groups:
                return ZoneMetrics(
                    zone_id=zone_id, zone_name=zone_name,
                    total_requests=0, cached_requests=0, total_bytes=0,
                    cached_bytes=0, http_2xx=0, http_3xx=0, http_4xx=0,
                    http_5xx=0, cache_hit_rate=0.0, error_rate=0.0, threats_blocked=0,
                )

            # Aggregate across all minute-groups in the window
            total = cached = tbytes = cbytes = threats = 0
            status_totals: dict = {}
            for g in groups:
                s = g["sum"]
                total += s.get("requests", 0)
                cached += s.get("cachedRequests", 0)
                tbytes += s.get("bytes", 0)
                cbytes += s.get("cachedBytes", 0)
                threats += s.get("threats", 0)
                for item in s.get("responseStatusMap", []):
                    code = item["edgeResponseStatus"]
                    status_totals[code] = status_totals.get(code, 0) + item["requests"]

            http_2xx = sum(v for k, v in status_totals.items() if 200 <= k < 300)
            http_3xx = sum(v for k, v in status_totals.items() if 300 <= k < 400)
            http_4xx = sum(v for k, v in status_totals.items() if 400 <= k < 500)
            http_5xx = sum(v for k, v in status_totals.items() if 500 <= k < 600)

            return ZoneMetrics(
                zone_id=zone_id,
                zone_name=zone_name,
                total_requests=total,
                cached_requests=cached,
                total_bytes=tbytes,
                cached_bytes=cbytes,
                http_2xx=http_2xx,
                http_3xx=http_3xx,
                http_4xx=http_4xx,
                http_5xx=http_5xx,
                cache_hit_rate=cached / total if total > 0 else 0.0,
                error_rate=http_5xx / total if total > 0 else 0.0,
                threats_blocked=threats,
            )
        except Exception:
            return None

    async def get_all_zones(self, since_minutes: int = 30) -> CloudflareStatus:
        if not self._configured:
            return CloudflareStatus()

        results = await asyncio.gather(
            *[self.get_zone_analytics(z, since_minutes) for z in self.zone_ids],
            return_exceptions=True,
        )
        zones = [r for r in results if isinstance(r, ZoneMetrics)]
        total_req = sum(z.total_requests for z in zones)
        total_5xx = sum(z.http_5xx for z in zones)
        total_cached = sum(z.cached_requests for z in zones)

        return CloudflareStatus(
            zones=zones,
            total_requests=total_req,
            total_5xx=total_5xx,
            overall_error_rate=total_5xx / total_req if total_req > 0 else 0.0,
            overall_cache_hit_rate=total_cached / total_req if total_req > 0 else 0.0,
        )


# Module-level singleton
cloudflare_service = CloudflareService()
