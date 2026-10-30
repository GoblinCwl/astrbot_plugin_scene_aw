from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .aw_client import ActivityWatchClient


MAIN_CATEGORIES = (
    "编程",
    "运维",
    "通讯",
    "浏览",
    "文档",
    "影音",
    "创作",
    "游戏",
    "其他",
)

CATEGORY_COLORS = {
    "编程": "#3B82F6",
    "运维": "#14B8A6",
    "通讯": "#22C55E",
    "浏览": "#F59E0B",
    "文档": "#6366F1",
    "影音": "#EC4899",
    "创作": "#A855F7",
    "游戏": "#EF4444",
    "其他": "#64748B",
}


class CategoryManager:
    def __init__(
        self,
        aw: ActivityWatchClient,
        category_sync_aw: ActivityWatchClient | None = None,
        bootstrap_days: int = 14,
        main_categories: tuple[str, ...] = MAIN_CATEGORIES,
    ):
        self.aw = aw
        # 此服务仅用于写入完整 category_sets，不参与读取和分类判断。
        self.category_sync_aw = category_sync_aw
        self.bootstrap_days = bootstrap_days
        self.main_categories = tuple(main_categories)

        self.category_sets: list[dict[str, Any]] = []
        self.active_set_ids: list[str] = []
        self.target_set_id: str | None = None
        self.categories: list[dict[str, Any]] = []
        self.apps: set[str] = set()

        self.known_apps: set[str] = set()
        self.unknown_apps: set[str] = set()
        self.app_titles: dict[str, list[str]] = {}
        self._write_lock = asyncio.Lock()

    async def bootstrap(
        self,
        window_bucket_id: str,
    ) -> None:
        await self.reload_categories()

        now = datetime.now(timezone.utc)
        start = now - timedelta(days=self.bootstrap_days)

        events = await self.aw.get_events(
            window_bucket_id,
            start,
            now,
        )

        self.apps = set()
        title_sets: dict[str, set[str]] = {}

        for event in events:
            data = event.get("data") or {}

            app = str(data.get("app", "")).strip()
            title = str(data.get("title", "")).strip()

            if not app:
                continue

            self.apps.add(app)

            if title:
                title_sets.setdefault(app, set()).add(title)

        self.app_titles = {
            app: sorted(titles)[:10]
            for app, titles in title_sets.items()
        }

        self._reclassify_apps()

    async def reload_categories(self) -> None:
        raw_sets = await self.aw.get_setting("category_sets")
        raw_active_ids = await self.aw.get_setting("active_set_ids")

        # ActivityWatch v0.14 两个接口均直接返回裸数组。
        self.category_sets = (
            [dict(item) for item in raw_sets if isinstance(item, dict)]
            if isinstance(raw_sets, list)
            else []
        )
        self.active_set_ids = (
            [str(item) for item in raw_active_ids]
            if isinstance(raw_active_ids, list)
            else []
        )

        sets_by_id = {
            str(item.get("id")): item
            for item in self.category_sets
            if item.get("id") is not None
        }
        self.target_set_id = next(
            (
                set_id
                for set_id in self.active_set_ids
                if set_id in sets_by_id
            ),
            None,
        )
        target_set = sets_by_id.get(self.target_set_id or "", {})
        target_categories = target_set.get("categories")
        self.categories = (
            [dict(item) for item in target_categories if isinstance(item, dict)]
            if isinstance(target_categories, list)
            else []
        )

        self._reclassify_apps()

    async def _write_target_categories(
        self,
        categories: list[dict[str, Any]],
    ) -> None:
        if self.target_set_id is None:
            raise RuntimeError("no active ActivityWatch CategorySet found")

        category_sets = [dict(item) for item in self.category_sets]
        target_found = False

        for index, category_set in enumerate(category_sets):
            if str(category_set.get("id")) != self.target_set_id:
                continue
            category_sets[index] = {
                **category_set,
                "categories": categories,
            }
            target_found = True
            break

        if not target_found:
            raise RuntimeError(
                f"active ActivityWatch CategorySet not found: {self.target_set_id}"
            )

        # v0.14 的 POST 会整体替换，必须回写完整 CategorySet 数组。
        await self.aw.update_setting("category_sets", category_sets)
        if self.category_sync_aw is not None:
            await self.category_sync_aw.update_setting(
                "category_sets",
                category_sets,
            )
            # 分类集定义本身不足以让 AW Web 参与统计；同步激活集，
            # 让目标服务器同样启用当前使用的 CategorySet。
            await self.category_sync_aw.update_setting(
                "active_set_ids",
                self.active_set_ids,
            )

    def _rule_regex(self, category: dict[str, Any]) -> str | None:
        rule = category.get("rule") or {}

        if str(rule.get("type", "")).lower() != "regex":
            return None

        # 兼容不同 AW / MCP 表达形式。
        value = (
            rule.get("regex")
            or rule.get("pattern")
            or rule.get("value")
        )

        return str(value) if value else None

    @staticmethod
    def _category_name(category: dict[str, Any]) -> str | None:
        name = category.get("name")
        if isinstance(name, list) and len(name) == 1:
            return str(name[0])
        if isinstance(name, str) and name:
            return name
        return None

    @staticmethod
    def _decode_exact_regex_token(token: str) -> str | None:
        result: list[str] = []
        index = 0
        regex_operators = set(".^$*+?{}[]()|")

        while index < len(token):
            if token.startswith("[.]", index):
                result.append(".")
                index += 3
                continue

            char = token[index]
            if char == "\\":
                if index + 1 >= len(token):
                    return None
                result.append(token[index + 1])
                index += 2
                continue

            if char in regex_operators:
                return None

            result.append(char)
            index += 1

        return "".join(result)

    def _exact_apps_from_category(
        self,
        category: dict[str, Any],
    ) -> set[str] | None:
        pattern = self._rule_regex(category)
        if pattern is None:
            return None
        if pattern == ".^":
            return set()
        if not pattern.startswith("^") or not pattern.endswith("$"):
            return None

        body = pattern[1:-1]
        if body.startswith("(?:") and body.endswith(")"):
            body = body[3:-1]

        tokens: list[str] = []
        start = 0
        escaped = False
        for index, char in enumerate(body):
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == "|":
                tokens.append(body[start:index])
                start = index + 1
        tokens.append(body[start:])

        apps: set[str] = set()
        for token in tokens:
            app = self._decode_exact_regex_token(token)
            if app is None or not app:
                return None
            apps.add(app)
        return apps

    @staticmethod
    def _exact_apps_rule(apps: set[str]) -> dict[str, str]:
        if not apps:
            return {"type": "regex", "regex": ".^"}

        escaped = [re.escape(app) for app in sorted(apps, key=str.casefold)]
        if len(escaped) == 1:
            pattern = f"^{escaped[0]}$"
        else:
            pattern = "^(?:" + "|".join(escaped) + ")$"
        return {"type": "regex", "regex": pattern}

    def _ensure_main_categories(
        self,
        categories: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        by_name = {
            name: category
            for category in categories
            if (name := self._category_name(category))
        }

        for name in self.main_categories:
            if name in by_name:
                continue
            category = {
                "name": [name],
                "rule": {"type": "regex", "regex": ".^"},
                "data": {
                    "color": CATEGORY_COLORS.get(name, "#64748B"),
                },
            }
            categories.append(category)
            by_name[name] = category

        return by_name

    async def _assign_categories(
        self,
        assignments: dict[str, str],
    ) -> None:
        async with self._write_lock:
            # v0.14 整体替换 CategorySet；每次写前必须重新读取，避免
            # 覆盖 AW Web 或另一个分类请求刚写入的修改。
            await self.reload_categories()
            await self._assign_categories_unlocked(assignments)

    async def _assign_categories_unlocked(
        self,
        assignments: dict[str, str],
    ) -> None:
        categories = [dict(category) for category in self.categories]
        by_name = self._ensure_main_categories(categories)
        regex_categories = [
            category
            for category in categories
            if self._rule_regex(category) is not None
        ]
        exact_apps = {
            id(category): self._exact_apps_from_category(category)
            for category in regex_categories
        }

        for app, target_name in assignments.items():
            app_key = app.casefold()
            target_category = by_name[target_name]

            for category in regex_categories:
                apps = exact_apps[id(category)]
                if apps is not None:
                    exact_apps[id(category)] = {
                        existing
                        for existing in apps
                        if existing.casefold() != app_key
                    }
                    continue

                pattern = self._rule_regex(category)
                try:
                    matches = bool(
                        pattern
                        and re.search(pattern, app, re.IGNORECASE)
                    )
                except re.error as exc:
                    raise ValueError(
                        f"invalid category regex: {pattern}"
                    ) from exc
                if matches and category is not target_category:
                    raise ValueError(
                        f"cannot safely remove {app} from a custom regex category"
                    )
                if category is target_category and not matches:
                    raise ValueError(
                        f"cannot safely add {app} to custom regex category {target_name}"
                    )

            target_apps = exact_apps[id(target_category)]
            if target_apps is not None:
                target_apps.add(app)

        for category in regex_categories:
            apps = exact_apps[id(category)]
            if apps is not None:
                category["rule"] = self._exact_apps_rule(apps)

        await self._write_target_categories(categories)
        await self.reload_categories()

    def observe_window_events(
        self,
        events: list[dict[str, Any]],
    ) -> set[str]:
        new_apps: set[str] = set()

        for event in events:
            data = event.get("data") or {}
            app = str(data.get("app", "")).strip()
            title = str(data.get("title", "")).strip()
            if not app:
                continue

            if app not in self.apps:
                self.apps.add(app)
                new_apps.add(app)

            if title:
                titles = self.app_titles.setdefault(app, [])
                if title not in titles:
                    titles.append(title)
                    if len(titles) > 10:
                        del titles[:-10]

        if new_apps:
            self._reclassify_apps()

        return new_apps

    def category_for_app(self, app: str) -> str | None:
        matches: list[tuple[int, str]] = []

        for category in self.categories:
            pattern = self._rule_regex(category)
            if not pattern:
                continue

            try:
                if not re.search(pattern, app, re.IGNORECASE):
                    continue
            except re.error:
                continue

            name = category.get("name")

            if isinstance(name, list):
                parts = [str(x) for x in name if x]
            elif name:
                parts = [str(name)]
            else:
                continue

            matches.append(
                (
                    len(parts),
                    " > ".join(parts),
                )
            )

        if not matches:
            return None

        # 优先更深层 Category。
        matches.sort(key=lambda x: x[0], reverse=True)
        return matches[0][1]

    def _reclassify_apps(self) -> None:
        self.known_apps.clear()
        self.unknown_apps.clear()

        for app in self.apps:
            if self.category_for_app(app):
                self.known_apps.add(app)
            else:
                self.unknown_apps.add(app)

    def unknown_app_samples(self) -> list[dict[str, Any]]:
        return [
            {
                "app": app,
                "titles": self.app_titles.get(app, [])[:5],
            }
            for app in sorted(self.unknown_apps)
        ]

    def dashboard_state(self) -> dict[str, Any]:
        top_level = set()

        for category in self.categories:
            name = category.get("name")

            if isinstance(name, list) and name:
                top_level.add(str(name[0]))
            elif isinstance(name, str) and name:
                top_level.add(name)

        configured_main = top_level.intersection(self.main_categories)

        known_app_items = []

        category_counts = {
            category: 0
            for category in self.main_categories
        }

        for app in sorted(self.known_apps):
            category = self.category_for_app(app)

            known_app_items.append(
                {
                    "app": app,
                    "category": category or "其他",
                }
            )

            top_category = (
                category.split(" > ", 1)[0]
                if category
                else "其他"
            )

            if top_category in category_counts:
                category_counts[top_category] += 1

        return {
            "initialized": True,
            "active_category_set_id": self.target_set_id,
            "category_names": list(self.main_categories),
            "total_apps": len(self.apps),
            "all_app_items": [
                {
                    "app": app,
                    "category": self.category_for_app(app),
                }
                for app in sorted(self.apps)
            ],
            "known_apps": len(self.known_apps),
            "known_app_items": known_app_items,
            "unknown_apps": [
                {"app": app}
                for app in sorted(self.unknown_apps)
            ],
            "category_count": len(configured_main),
            "main_categories": [
                {
                    "name": category,
                    "app_count": category_counts[category],
                    "configured": category in configured_main,
                }
                for category in self.main_categories
            ],
        }

    async def assign_category(self, app: str, category: str) -> None:
        if category not in self.main_categories:
            raise ValueError("invalid category")

        await self._assign_categories({app: category})
        self.apps.add(app)
        self._reclassify_apps()

    async def apply_suggestions(
        self,
        suggestions: list[dict[str, Any]],
        min_confidence: float,
    ) -> int:
        accepted: dict[str, tuple[str, str, float]] = {}

        for item in suggestions:
            app = str(item.get("app", "")).strip()
            category = str(item.get("category", "")).strip()

            try:
                confidence = float(item.get("confidence", 0))
            except Exception:
                confidence = 0.0

            if not app:
                continue

            if category not in self.main_categories:
                continue

            if confidence < min_confidence:
                continue

            key = app.casefold()
            previous = accepted.get(key)
            if previous is None or confidence > previous[2]:
                accepted[key] = (app, category, confidence)

        applied = len(accepted)

        if not applied:
            return 0

        assignments = {
            app: category
            for app, category, _confidence in accepted.values()
        }
        await self._assign_categories(assignments)

        return applied
