from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from .aw_client import ActivityWatchClient


class ActivityCollector:
    def __init__(self, aw: ActivityWatchClient):
        self.aw = aw
        self.buckets: dict[str, list[str]] = {}

    async def discover_buckets(self) -> dict[str, list[str]]:
        raw = await self.aw.get_buckets()

        groups: dict[str, list[str]] = {
            "window": [],
            "afk": [],
            "web": [],
            "editor": [],
            "enhanced": [],
            "enhanced_snapshot": [],
        }

        for bucket_id, meta in raw.items():
            bucket_type = meta.get("type", "")
            client = meta.get("client", "")

            if client == "aw-watcher-window":
                groups["window"].append(bucket_id)

            elif client == "aw-watcher-afk":
                groups["afk"].append(bucket_id)

            elif bucket_type == "web.tab.current" or client == "aw-client-web":
                groups["web"].append(bucket_id)

            elif bucket_type == "app.editor.activity":
                groups["editor"].append(bucket_id)

            elif bucket_type == "enrichment-snapshot":
                groups["enhanced_snapshot"].append(bucket_id)

            elif client == "aw-watcher-enhanced":
                groups["enhanced"].append(bucket_id)

        self.buckets = {
            name: ids
            for name, ids in groups.items()
            if ids
        }

        return self.buckets

    async def collect(
        self,
        start: datetime,
        end: datetime,
    ) -> dict[str, list[dict[str, Any]]]:

        jobs = []
        keys = []

        for group, bucket_ids in self.buckets.items():
            for bucket_id in bucket_ids:
                keys.append(group)
                jobs.append(
                    self.aw.get_events(
                        bucket_id,
                        start,
                        end,
                    )
                )

        results = await asyncio.gather(
            *jobs,
            return_exceptions=True,
        )

        if results and all(isinstance(result, Exception) for result in results):
            raise ConnectionError("all ActivityWatch bucket requests failed")

        merged: dict[str, list[dict[str, Any]]] = {
            name: []
            for name in self.buckets
        }

        for group, result in zip(keys, results):
            if isinstance(result, Exception):
                continue

            merged[group].extend(result)

        for events in merged.values():
            events.sort(
                key=lambda x: x.get("timestamp", "")
            )

        return merged
