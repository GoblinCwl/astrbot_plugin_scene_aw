from __future__ import annotations

from typing import Any

import aiohttp


class AppWebLookup:
    """通过 Tavily 按应用文件名查询公开网页。"""

    def __init__(self, api_key: str = ""):
        self.api_key = str(api_key).strip()

    async def search(
        self,
        app: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        if not self.api_key:
            raise ValueError("未配置 Tavily API Key")

        timeout = aiohttp.ClientTimeout(total=20)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "query": f'"{app}" software application executable',
            "topic": "general",
            "search_depth": "basic",
            "max_results": max(1, min(8, int(limit))),
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }

        async with aiohttp.ClientSession(
            timeout=timeout,
            trust_env=True,
        ) as session:
            async with session.post(
                "https://api.tavily.com/search",
                headers=headers,
                json=body,
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json()

        results = []
        for item in payload.get("results", []):
            if not isinstance(item, dict):
                continue

            title = str(item.get("title") or "").strip()
            content = str(item.get("content") or "").strip()
            url = str(item.get("url") or "").strip()
            if not title and not content:
                continue

            results.append({
                "title": title[:240],
                "summary": content[:700],
                "source": url[:500],
                "relevance": round(
                    max(0.0, min(1.0, float(item.get("score") or 0))),
                    4,
                ),
            })

        return results
