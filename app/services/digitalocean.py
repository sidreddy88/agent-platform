"""
Digital Ocean service — droplet health + WordPress site HTTP checks.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

import httpx

from app.core.config import settings


@dataclass
class DropletStatus:
    id: int
    name: str
    status: str  # "active" | "off" | "archive"
    region: str
    size: str
    ip_address: Optional[str]
    memory_mb: int
    vcpus: int
    created_at: str


@dataclass
class SiteHealth:
    site_url: str
    droplet_id: int
    droplet_name: str
    status_code: Optional[int]
    response_time_ms: Optional[float]
    is_healthy: bool
    error: Optional[str]
    checked_at: datetime


class DigitalOceanService:
    BASE_URL = "https://api.digitalocean.com/v2"

    def __init__(self):
        self.token = getattr(settings, "do_api_token", "")

    @property
    def _configured(self) -> bool:
        return bool(self.token)

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def get_droplets(self) -> List[DropletStatus]:
        if not self._configured:
            return []
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.BASE_URL}/droplets",
                headers=self._headers,
                params={"per_page": 200},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

        droplets = []
        for d in data.get("droplets", []):
            networks = d.get("networks", {})
            v4 = networks.get("v4", [])
            ip = next((n["ip_address"] for n in v4 if n["type"] == "public"), None)
            droplets.append(
                DropletStatus(
                    id=d["id"],
                    name=d["name"],
                    status=d["status"],
                    region=d["region"]["slug"],
                    size=d["size_slug"],
                    ip_address=ip,
                    memory_mb=d["memory"],
                    vcpus=d["vcpus"],
                    created_at=d["created_at"],
                )
            )
        return droplets

    async def get_droplet_metrics(self, droplet_id: int) -> dict:
        """Fetch load and memory metrics for a droplet via DO Monitoring API."""
        if not self._configured:
            return {}
        import time
        end = int(time.time())
        start = end - 3600  # last hour

        async def _fetch(metric_type: str) -> float | None:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"{self.BASE_URL}/monitoring/metrics/droplet/{metric_type}",
                        headers=self._headers,
                        params={"host_id": str(droplet_id), "start": str(start), "end": str(end)},
                        timeout=10,
                    )
                    if resp.status_code != 200:
                        return None
                    results = resp.json().get("data", {}).get("result", [])
                    if results and results[0].get("values"):
                        return float(results[0]["values"][-1][1])
                    return None
            except Exception:
                return None

        load_1, mem_available, mem_total = await asyncio.gather(
            _fetch("load_1"),
            _fetch("memory_available"),
            _fetch("memory_total"),
        )

        memory_percent = None
        if mem_available is not None and mem_total and mem_total > 0:
            memory_percent = round((1 - mem_available / mem_total) * 100, 1)

        return {
            "load_1": round(load_1, 2) if load_1 is not None else None,
            "memory_percent": memory_percent,
        }

    async def check_site(
        self, url: str, droplet_id: int = 0, droplet_name: str = "unknown"
    ) -> SiteHealth:
        """HTTP health check for a single WordPress site."""
        start = datetime.utcnow()
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                resp = await client.get(url, timeout=10)
            elapsed = (datetime.utcnow() - start).total_seconds() * 1000
            is_healthy = resp.status_code < 500
            return SiteHealth(
                site_url=url,
                droplet_id=droplet_id,
                droplet_name=droplet_name,
                status_code=resp.status_code,
                response_time_ms=elapsed,
                is_healthy=is_healthy,
                error=None if is_healthy else f"HTTP {resp.status_code}",
                checked_at=start,
            )
        except Exception as exc:
            elapsed = (datetime.utcnow() - start).total_seconds() * 1000
            return SiteHealth(
                site_url=url,
                droplet_id=droplet_id,
                droplet_name=droplet_name,
                status_code=None,
                response_time_ms=elapsed,
                is_healthy=False,
                error=str(exc),
                checked_at=start,
            )

    async def check_all_sites(self, sites: List[dict]) -> List[SiteHealth]:
        """
        sites: [{"url": str, "droplet_id": int, "droplet_name": str}]
        """
        tasks = [
            self.check_site(s["url"], s.get("droplet_id", 0), s.get("droplet_name", "unknown"))
            for s in sites
        ]
        return await asyncio.gather(*tasks)


# Module-level singleton
do_service = DigitalOceanService()
