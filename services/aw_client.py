from __future__ import annotations

from datetime import datetime
from typing import Any

import aiohttp


class ActivityWatchClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def _get(self, path: str, params: dict[str, Any] | None = None):
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{self.base_url}{path}",
                params=params,
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def _post(self, path: str, data: Any):
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.base_url}{path}",
                json=data,
            ) as resp:
                resp.raise_for_status()

                if resp.content_length == 0:
                    return None

                try:
                    return await resp.json()
                except Exception:
                    return await resp.text()

    async def get_buckets(self) -> dict[str, Any]:
        return await self._get("/api/0/buckets/")

    async def get_setting(self, key: str):
        return await self._get(f"/api/0/settings/{key}")

    async def update_setting(self, key: str, value: Any):
        return await self._post(
            f"/api/0/settings/{key}",
            value,
        )

    async def get_events(
        self,
        bucket_id: str,
        start: datetime,
        end: datetime,
        limit: int = -1,
    ) -> list[dict[str, Any]]:
        return await self._get(
            f"/api/0/buckets/{bucket_id}/events",
            params={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": limit,
            },
        )
