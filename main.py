from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.agent.message import TextPart

from .services.aw_client import ActivityWatchClient
from .services.app_web_lookup import AppWebLookup
from .services.collector import ActivityCollector
from .services.category_manager import CategoryManager
from .services.category_classifier import (
    CategoryClassifier,
    parse_category_definitions,
)
from .services.state_store import SceneState, SemanticThread
from .services.scene_detector import SceneDetector
from .services.scene_interpreter import SceneInterpreter


PLUGIN_NAME = "astrbot_plugin_scene_aw"


class SceneAwPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)

        self.config = config or {}

        self.scan_interval = max(
            10,
            int(self.config.get("scan_interval_seconds", 30)),
        )

        self.startup_lookback_minutes = max(
            1,
            int(self.config.get("startup_lookback_minutes", 10)),
        )
        self.context_injection_enabled = bool(
            self.config.get("context_injection_enabled", True)
        )
        self.new_app_notification_enabled = bool(
            self.config.get("new_app_notification_enabled", False)
        )
        self.new_app_notification_umo = str(
            self.config.get(
                "new_app_notification_unified_msg_origin",
                "",
            )
            or ""
        ).strip()
        self.new_app_notification_quiet_seconds = max(
            60,
            int(
                self.config.get(
                    "new_app_notification_quiet_minutes",
                    3,
                )
            ) * 60,
        )
        self.new_app_notification_template = str(
            self.config.get(
                "new_app_notification_template",
                "",
            )
            or ""
        ).strip()
        logger.info(
            "[scene_aw] context injection enabled=%s",
            self.context_injection_enabled,
        )

        self.started_at = datetime.now(timezone.utc)

        self.aw = ActivityWatchClient(
            self.config.get(
                "aw_base_url",
                "http://127.0.0.1:5600",
            )
        )

        self.collector = ActivityCollector(self.aw)

        self.category_definitions = parse_category_definitions(
            str(self.config.get("category_definitions", ""))
        )
        self.category_llm_prompt = str(
            self.config.get("category_llm_prompt", "") or ""
        ).strip()
        self.scene_llm_prompt = str(
            self.config.get("scene_llm_prompt", "") or ""
        ).strip()

        if not self.category_llm_prompt:
            logger.error(
                "[scene_aw] Category LLM prompt is empty; "
                "all Category LLM classification is disabled"
            )
        if not self.scene_llm_prompt:
            logger.error(
                "[scene_aw] Scene LLM prompt is empty; "
                "Scene interpretation is disabled"
            )

        self.category_manager = CategoryManager(
            self.aw,
            bootstrap_days=int(
                self.config.get("category_bootstrap_days", 14)
            ),
            main_categories=tuple(self.category_definitions),
        )

        self.category_classifier = CategoryClassifier(
            context,
            str(
                self.config.get(
                    "category_llm_provider_id",
                    "",
                )
            ),
            prompt_template=self.category_llm_prompt,
            category_definitions=self.category_definitions,
        )
        self.app_web_lookup = AppWebLookup(
            str(self.config.get("tavily_api_key", ""))
        )

        self.state = SceneState()

        self.scene_detector = SceneDetector(
            hold_seconds=int(
                self.config.get(
                    "scene_candidate_hold_seconds",
                    90,
                )
            ),
            llm_min_interval_seconds=int(
                self.config.get(
                    "scene_llm_min_interval_seconds",
                    300,
                )
            ),
            profile_window_seconds=int(
                self.config.get(
                    "scene_profile_window_seconds",
                    300,
                )
            ),
            profile_change_threshold=float(
                self.config.get(
                    "scene_profile_change_threshold",
                    0.35,
                )
            ),
        )

        self.scene_interpreter = SceneInterpreter(
            context,
            str(
                self.config.get(
                    "scene_llm_provider_id",
                    "",
                )
            ),
            max_scenes=int(self.config.get("max_threads", 3)),
            prompt_template=self.scene_llm_prompt,
        )

        self.aw_connected = False
        self.bucket_count = 0
        self.last_aw_event_at: datetime | None = None
        self.aw_offline_after_seconds = max(
            self.scan_interval * 3,
            int(self.config.get("aw_offline_after_seconds", 180)),
        )

        self._runtime_lock = asyncio.Lock()
        self._category_classify_lock = asyncio.Lock()
        self._scan_task: asyncio.Task | None = None
        self._stopping = False
        self._new_app_batch: dict[str, str] = {}
        self._new_app_batch_last_added_at: datetime | None = None
        self._active_category_confirmations: dict[
            str,
            dict[str, Any],
        ] = {}
        self._notification_state_path = (
            Path(__file__).resolve().parent
            / "data"
            / "new_app_notifications.json"
        )
        self._notified_apps = self._load_notified_apps()

        self.previous_scene = None
        self.last_llm = None

        self.category_state = {
            "initialized": False,
            "known_apps": 0,
            "unknown_apps": [],
            "category_count": 0,
            "suggestions": [],
        }

        self.communication_state = {
            "active": False,
            "active_seconds": 0,
            "apps": [],
        }

        self.recent_activities: list[dict[str, Any]] = []
        self.enhanced_context_events: dict[
            tuple[str, Any],
            dict[str, Any],
        ] = {}

        context.register_web_api(
            f"/{PLUGIN_NAME}/dashboard/state",
            self.dashboard_state,
            ["GET"],
            "Scene Awareness dashboard state",
        )

        context.register_web_api(
            f"/{PLUGIN_NAME}/dashboard/confirm-category",
            self.confirm_category,
            ["POST"],
            "Confirm application category",
        )

        context.register_web_api(
            f"/{PLUGIN_NAME}/dashboard/classify-categories",
            self.classify_categories,
            ["POST"],
            "Classify unknown applications with LLM",
        )

        context.register_web_api(
            f"/{PLUGIN_NAME}/dashboard/classify-categories-web",
            self.classify_categories_web,
            ["POST"],
            "Classify pending applications with web evidence",
        )

        logger.info("[scene_aw] plugin loaded")
        logger.info(
            "[scene_aw] new app notification enabled=%s umo_configured=%s "
            "quiet_minutes=%d notified_apps=%d",
            self.new_app_notification_enabled,
            bool(self.new_app_notification_umo),
            self.new_app_notification_quiet_seconds // 60,
            len(self._notified_apps),
        )
        if (
            self.new_app_notification_enabled
            and not self.new_app_notification_umo
        ):
            logger.warning(
                "[scene_aw] new app notification is enabled but "
                "new_app_notification_unified_msg_origin is empty"
            )

        # 热重载插件时通常已经存在 running loop，
        # 因此直接尝试启动后台扫描器。
        try:
            asyncio.get_running_loop().create_task(
                self._ensure_runtime_started()
            )
        except RuntimeError:
            pass

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        # 冷启动 AstrBot 时补一次。
        await self._ensure_runtime_started()

    async def _ensure_runtime_started(self):
        if self._scan_task and not self._scan_task.done():
            return

        async with self._runtime_lock:
            if self._scan_task and not self._scan_task.done():
                return

            try:
                raw_buckets = await self.aw.get_buckets()

                self.aw_connected = True
                self.bucket_count = len(raw_buckets)

                await self.collector.discover_buckets()

                # 扫描器优先启动，Category/LLM 永远不能阻塞实时状态。
                self._scan_task = asyncio.create_task(
                    self._scan_loop()
                )

                logger.info(
                    "[scene_aw] scanner started, buckets=%d groups=%s",
                    self.bucket_count,
                    ",".join(sorted(self.collector.buckets.keys())),
                )

                # Category 初始化单独后台运行。
                asyncio.create_task(
                    self._initialize_categories()
                )

            except Exception:
                self.aw_connected = False
                logger.exception(
                    "[scene_aw] failed to start scanner"
                )

    async def _initialize_categories(self):
        logger.info("[scene_aw] category initialization started")

        try:
            window_buckets = self.collector.buckets.get(
                "window",
                [],
            )

            if not window_buckets:
                return

            await self.category_manager.bootstrap(
                window_buckets[0]
            )

            logger.info(
                "[scene_aw] category bootstrap finished: apps=%d",
                len(self.category_manager.apps),
            )

            self.category_state = (
                self.category_manager.dashboard_state()
            )
            self.category_state["initialized"] = False
            self.scene_detector.reclassify(
                self.category_manager.category_for_app
            )

            provider_id = str(
                self.config.get(
                    "category_llm_provider_id",
                    "",
                )
            )

            suggestions = []

            classify_on_startup = bool(
                self.config.get(
                    "category_classify_on_startup",
                    False,
                )
            )

            if (
                provider_id
                and self.category_llm_prompt
                and classify_on_startup
            ):
                samples = (
                    self.category_manager
                    .unknown_app_samples()
                )

                logger.info(
                    "[scene_aw] category AI started: apps=%d provider=%s",
                    len(samples),
                    provider_id,
                )

                for i in range(0, len(samples), 20):
                    batch = samples[i:i + 20]

                    try:
                        suggestions.extend(
                            await self.category_classifier
                            .classify_batch(batch)
                        )
                    except Exception:
                        logger.exception(
                            "[scene_aw] category AI classification failed"
                        )

            self.category_state["suggestions"] = suggestions

            applied = 0

            if suggestions:
                threshold = float(
                    self.config.get(
                        "category_auto_apply_confidence",
                        0.9,
                    )
                )

                applied = await self.category_manager.apply_suggestions(
                    suggestions,
                    threshold,
                )

                # 根据写回后的真实 AW Categories 重新计算。
                self.category_manager._reclassify_apps()
                self.scene_detector.reclassify(
                    self.category_manager.category_for_app
                )

                self.category_state = (
                    self.category_manager.dashboard_state()
                )

                # 低置信度建议仍保留给 Dashboard 查看。
                remaining = {
                    item["app"]
                    for item in self.category_state["unknown_apps"]
                }

                self.category_state["suggestions"] = [
                    item
                    for item in suggestions
                    if item.get("app") in remaining
                ]

            self.category_state["initialized"] = True

            logger.info(
                "[scene_aw] categories initialized: "
                "known=%d unknown=%d suggested=%d applied=%d",
                self.category_state["known_apps"],
                len(self.category_state["unknown_apps"]),
                len(suggestions),
                applied,
            )

        except Exception:
            logger.exception(
                "[scene_aw] category initialization failed"
            )

    async def _scan_loop(self):
        cursor = (
            datetime.now(timezone.utc)
            - timedelta(minutes=self.startup_lookback_minutes)
        )

        startup_scene_pending = bool(
            self.config.get("scene_llm_on_startup", True)
        )

        while not self._stopping:
            started = time.monotonic()

            try:
                now = datetime.now(timezone.utc)

                data = await self.collector.collect(
                    cursor,
                    now,
                )
                self._record_recent_activities(data)

                self.aw_connected = True
                previous_aw_status = self.state.aw_status
                latest_event_at = self._latest_event_at(data)
                if latest_event_at:
                    self.last_aw_event_at = max(
                        latest_event_at,
                        self.last_aw_event_at or latest_event_at,
                    )
                heartbeat_fresh = bool(
                    self.last_aw_event_at
                    and (now - self.last_aw_event_at).total_seconds()
                    <= self.aw_offline_after_seconds
                )
                self.state.aw_status = "ACTIVE" if heartbeat_fresh else "OFFLINE"

                force_scene_reason = None
                if startup_scene_pending:
                    force_scene_reason = "startup"
                elif previous_aw_status == "OFFLINE" and heartbeat_fresh:
                    force_scene_reason = "returned_from_offline"

                startup_attempted = await self._apply_scan(
                    data=data,
                    now=now,
                    force_scene_reason=force_scene_reason,
                )
                if startup_attempted:
                    startup_scene_pending = False

                self.state.last_scan_at = now
                self.state.updated_at = now

                cursor = now
            except asyncio.CancelledError:
                break

            except Exception:
                self.aw_connected = False
                self.state.aw_status = "OFFLINE"
                logger.exception("[scene_aw] scan failed")

            elapsed = time.monotonic() - started
            wait = max(1.0, self.scan_interval - elapsed)

            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                break

    async def _apply_scan(
        self,
        data: dict[str, list[dict[str, Any]]],
        now: datetime,
        force_scene_reason: str | None = None,
    ):
        new_apps = self.category_manager.observe_window_events(
            data.get("window", [])
        )
        unknown_keys = {
            self._app_key(app)
            for app in self.category_manager.unknown_apps
        }
        self._active_category_confirmations = {
            key: item
            for key, item in self._active_category_confirmations.items()
            if key in unknown_keys
        }
        self._new_app_batch = {
            key: app
            for key, app in self._new_app_batch.items()
            if key in unknown_keys
        }
        if not self._new_app_batch:
            self._new_app_batch_last_added_at = None
        if new_apps and self.category_state.get("initialized"):
            suggestions = self.category_state.get("suggestions", [])
            self.category_state = self.category_manager.dashboard_state()
            remaining = self.category_manager.unknown_apps
            self.category_state["suggestions"] = [
                item
                for item in suggestions
                if item.get("app") in remaining
            ]
            logger.info(
                "[scene_aw] new applications discovered: %s",
                ", ".join(sorted(new_apps)),
            )
            self._queue_new_unknown_apps(new_apps, now)

        await self._maybe_send_new_app_notification(now)

        self._update_focus(data.get("window", []), now)
        was_afk = self.state.afk
        self._update_afk(data.get("afk", []))
        if self.state.afk and not was_afk:
            self._clear_current_scene_for_afk(now)
        elif was_afk and not self.state.afk:
            force_scene_reason = "returned_from_afk"
        self._update_enhanced_context(data, now)

        if self.state.aw_status != "OFFLINE":
            self.scene_detector.add_events(
                window_events=data.get("window", []),
                afk_events=data.get("afk", []),
                category_for_app=self.category_manager.category_for_app,
                now=now,
            )

        self.state.category_profile = self.scene_detector.current_profile(now)
        self.communication_state = self.scene_detector.communication_summary(
            now,
            active_within_seconds=max(60, self.scan_interval * 2),
        )
        if self.state.aw_status != "OFFLINE":
            self.state.aw_status = "AFK" if self.state.afk else "ACTIVE"

        return await self._maybe_interpret_scene(
            force_reason=force_scene_reason
        )

    @staticmethod
    def _app_key(app: str) -> str:
        return app.strip().casefold()

    def _load_notified_apps(self) -> set[str]:
        try:
            raw = json.loads(
                self._notification_state_path.read_text(encoding="utf-8")
            )
            apps = raw.get("notified_apps", []) if isinstance(raw, dict) else []
            return {
                self._app_key(str(app))
                for app in apps
                if str(app).strip()
            }
        except FileNotFoundError:
            return set()
        except Exception:
            logger.exception(
                "[scene_aw] failed to load new app notification state"
            )
            return set()

    def _save_notified_apps(self) -> None:
        path = self._notification_state_path
        temporary = path.with_suffix(".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(
                    {"notified_apps": sorted(self._notified_apps)},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except Exception:
            logger.exception(
                "[scene_aw] failed to save new app notification state"
            )

    def _queue_new_unknown_apps(
        self,
        apps: set[str],
        now: datetime,
    ) -> None:
        if not self.new_app_notification_enabled:
            return

        queued = []
        for app in sorted(apps, key=str.casefold):
            key = self._app_key(app)
            if (
                app not in self.category_manager.unknown_apps
                or key in self._notified_apps
                or key in self._active_category_confirmations
                or key in self._new_app_batch
            ):
                continue
            self._new_app_batch[key] = app
            queued.append(app)

        if queued:
            self._new_app_batch_last_added_at = now
            logger.info(
                "[scene_aw] new unclassified apps queued for notification: %s",
                ", ".join(queued),
            )

    def _merge_category_suggestions(
        self,
        suggestions: list[dict[str, Any]],
    ) -> None:
        by_key = {
            self._app_key(str(item.get("app", ""))): item
            for item in self.category_state.get("suggestions", [])
            if str(item.get("app", "")).strip()
        }
        for item in suggestions:
            app = str(item.get("app", "")).strip()
            if app:
                by_key[self._app_key(app)] = item
        self.category_state["suggestions"] = list(by_key.values())

    async def _maybe_send_new_app_notification(
        self,
        now: datetime,
    ) -> None:
        if (
            not self.new_app_notification_enabled
            or not self._new_app_batch
            or not self._new_app_batch_last_added_at
        ):
            return
        if not self.new_app_notification_umo:
            return
        if (
            now - self._new_app_batch_last_added_at
        ).total_seconds() < self.new_app_notification_quiet_seconds:
            return
        if self._category_classify_lock.locked():
            return

        apps = [
            app
            for app in self._new_app_batch.values()
            if app in self.category_manager.unknown_apps
        ]
        if not apps:
            self._new_app_batch.clear()
            self._new_app_batch_last_added_at = None
            return

        suggestions: list[dict[str, Any]] = []
        if (
            self.app_web_lookup.api_key
            and self.category_classifier.provider_id
            and self.category_llm_prompt
        ):
            logger.info(
                "[scene_aw] proactive Tavily lookup started: "
                "apps=%d concurrency=3",
                len(apps),
            )
            semaphore = asyncio.Semaphore(3)

            async def lookup(app: str) -> dict[str, Any] | None:
                async with semaphore:
                    try:
                        evidence = await self.app_web_lookup.search(app)
                    except Exception:
                        logger.exception(
                            "[scene_aw] proactive Tavily lookup failed: %s",
                            app,
                        )
                        return None
                if not evidence:
                    logger.warning(
                        "[scene_aw] proactive Tavily lookup returned no "
                        "results: %s",
                        app,
                    )
                    return None
                logger.info(
                    "[scene_aw] proactive Tavily lookup finished: "
                    "app=%s results=%d",
                    app,
                    len(evidence),
                )
                return {
                    "app": app,
                    "titles": self.category_manager.app_titles.get(app, [])[:5],
                    "web_evidence": evidence,
                }

            try:
                samples = [
                    sample
                    for sample in await asyncio.gather(
                        *(lookup(app) for app in apps)
                    )
                    if sample is not None
                ]
                logger.info(
                    "[scene_aw] proactive Category LLM started after "
                    "Tavily: searched=%d total=%d",
                    len(samples),
                    len(apps),
                )
                async with self._category_classify_lock:
                    for index in range(0, len(samples), 20):
                        suggestions.extend(
                            await self.category_classifier.classify_batch(
                                samples[index:index + 20]
                            )
                        )
            except Exception:
                logger.exception(
                    "[scene_aw] proactive web-assisted Category LLM failed; "
                    "notification will be sent without suggestions"
                )
                suggestions = []
        else:
            logger.warning(
                "[scene_aw] proactive web-assisted classification unavailable: "
                "tavily=%s provider=%s prompt=%s; notification will be sent "
                "without suggestions",
                bool(self.app_web_lookup.api_key),
                bool(self.category_classifier.provider_id),
                bool(self.category_llm_prompt),
            )

        suggestions_by_key = {
            self._app_key(str(item.get("app", ""))): item
            for item in suggestions
            if str(item.get("app", "")).strip()
        }
        items = []
        confirmations: dict[str, dict[str, Any]] = {}
        pending_suggestions: list[dict[str, Any]] = []
        for index, app in enumerate(apps, 1):
            suggestion = suggestions_by_key.get(self._app_key(app))
            category = str((suggestion or {}).get("category", "")).strip()
            confidence = float((suggestion or {}).get("confidence", 0) or 0)
            valid = category in self.category_manager.main_categories
            suggestion_text = (
                f"{category}（{confidence:.0%}）" if valid else "暂无"
            )
            items.append(f"{index}. {app}\n   建议：{suggestion_text}")
            confirmations[self._app_key(app)] = {
                "app": app,
                "suggested_category": category if valid else None,
                "confidence": confidence if valid else None,
                "notified_at": now.isoformat(),
            }
            pending_suggestions.append({
                "app": app,
                "category": category if valid else "其他",
                "confidence": confidence if valid else 0.0,
            })

        template = self.new_app_notification_template or (
            "发现以下 {count} 个未分类应用：\n\n{items}\n\n"
            "请确认建议分类，或者告诉我要调整成什么分类。"
        )
        try:
            message = template.format(
                count=len(apps),
                items="\n\n".join(items),
                categories="、".join(self.category_manager.main_categories),
            )
        except (KeyError, ValueError):
            logger.exception(
                "[scene_aw] invalid new app notification template"
            )
            return

        logger.info(
            "[scene_aw] sending new app notification: umo=%s apps=%d",
            self.new_app_notification_umo,
            len(apps),
        )
        try:
            sent = await self.context.send_message(
                self.new_app_notification_umo,
                MessageChain().message(message),
            )
            if not sent:
                raise RuntimeError("platform unavailable")
        except Exception:
            logger.exception(
                "[scene_aw] failed to send new app notification; batch retained"
            )
            return

        self._merge_category_suggestions(pending_suggestions)
        self._active_category_confirmations.update(confirmations)
        for app in apps:
            key = self._app_key(app)
            self._notified_apps.add(key)
            self._new_app_batch.pop(key, None)
        self._new_app_batch_last_added_at = None
        self._save_notified_apps()
        logger.info(
            "[scene_aw] new app notification sent: apps=%s",
            ", ".join(apps),
        )

    def _update_enhanced_context(
        self,
        data: dict[str, list[dict[str, Any]]],
        now: datetime,
    ) -> None:
        for group in ("enhanced", "enhanced_snapshot"):
            for event in data.get(group, []):
                event_id = event.get("id")
                key = (
                    group,
                    event_id
                    if event_id is not None
                    else (
                        event.get("timestamp"),
                        repr(event.get("data")),
                    ),
                )
                self.enhanced_context_events[key] = {
                    "source": group,
                    **event,
                }

        cutoff = now - timedelta(
            seconds=self.scene_detector.profile_window_seconds
        )
        retained = {}

        for key, event in self.enhanced_context_events.items():
            try:
                timestamp = datetime.fromisoformat(
                    str(event.get("timestamp", "")).replace(
                        "Z",
                        "+00:00",
                    )
                )
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue

            if timestamp >= cutoff:
                retained[key] = event

        self.enhanced_context_events = retained
        self._attach_current_focus_enhanced()

    def _attach_current_focus_enhanced(self) -> None:
        focus = self.state.current_focus
        if not focus:
            return

        focus.pop("enhanced", None)
        focus_app = str(focus.get("app") or "").casefold()
        focus_title = str(focus.get("title") or "").strip()
        enhanced = None

        for event in sorted(
            self.enhanced_context_events.values(),
            key=lambda item: item.get("timestamp", ""),
        ):
            data = event.get("data") or {}
            if (
                str(data.get("app") or "").casefold() != focus_app
                or str(data.get("title") or "").strip() != focus_title
            ):
                continue

            if enhanced is None or len(data) > 2:
                enhanced = dict(data)

        if enhanced:
            focus["enhanced"] = enhanced

    def _context_injection_payload(self) -> dict[str, Any] | None:
        if (
            not self.context_injection_enabled
            or self.state.aw_status != "ACTIVE"
            or self.state.afk
        ):
            return None

        primary = next(
            (
                item
                for item in self.state.semantic_threads
                if item.role == "primary"
            ),
            None,
        )
        duration_minutes = None
        if primary and primary.started_at:
            duration_minutes = max(
                0,
                round(
                    (
                        datetime.now(timezone.utc) - primary.started_at
                    ).total_seconds() / 60
                ),
            )

        payload: dict[str, Any] = {
            "updated_at": self.state.updated_at.isoformat(),
        }
        if primary:
            payload["primary_scene"] = {
                "name": primary.name,
                "duration_minutes": duration_minutes,
            }
            payload["secondary_scenes"] = [
                item.name
                for item in self.state.semantic_threads
                if item.role == "secondary"
            ]

        category_summary = self.scene_detector.activity_clusters()[:4]
        if category_summary:
            payload["category_summary"] = [
                {
                    "category": item.get("category"),
                    "active_seconds": round(
                        float(item.get("active_seconds") or 0)
                    ),
                }
                for item in category_summary
            ]

        focus = self.state.current_focus
        if focus and focus.get("category"):
            payload["focus_category"] = focus["category"]

        return payload

    def _format_context_injection(
        self,
        payload: dict[str, Any],
    ) -> str:
        lines = [
            "<user_activity>",
            "仅作背景参考；与当前话题无关时不要提及：",
        ]

        updated_at = payload.get("updated_at")
        if updated_at:
            try:
                local_time = datetime.fromisoformat(str(updated_at)).astimezone()
                lines.append(f"时间：{local_time:%Y-%m-%d %H:%M:%S}")
            except (TypeError, ValueError):
                lines.append(f"时间：{updated_at}")

        primary = payload.get("primary_scene") or {}
        if primary.get("name"):
            duration = primary.get("duration_minutes")
            suffix = (
                f"（持续约 {duration} 分钟）"
                if duration is not None
                else ""
            )
            lines.append(f"事务：{primary['name']}{suffix}")

        secondary = payload.get("secondary_scenes") or []
        if secondary:
            lines.append("并行：" + "、".join(map(str, secondary)))

        if payload.get("focus_category"):
            lines.append(f"焦点分类：{payload['focus_category']}")

        categories = payload.get("category_summary") or []
        category_parts = [
            f"{item.get('category')} {round(float(item.get('active_seconds') or 0))} 秒"
            for item in categories
            if item.get("category")
        ]
        if category_parts:
            lines.append("近况：" + "、".join(category_parts))

        lines.append("</user_activity>")
        return "\n".join(lines)

    @filter.on_llm_request()
    async def inject_scene_context(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ):
        payload = self._context_injection_payload()
        if payload:
            text = self._format_context_injection(payload)
            req.extra_user_content_parts.append(
                TextPart(text=text).mark_as_temp()
            )
            logger.info(
                "[scene_aw] context injected:\n%s",
                text,
            )

        if (
            event.unified_msg_origin == self.new_app_notification_umo
            and self._active_category_confirmations
        ):
            pending = list(self._active_category_confirmations.values())
            text = (
                "<pending_app_category_confirmation>\n"
                "以下应用已向用户询问分类。仅当用户本轮明确确认、修改或拒绝时，"
                "调用对应 scene_aw 工具；不要把沉默或无关回复当作确认：\n"
                f"{json.dumps(pending, ensure_ascii=False, separators=(',', ':'))}\n"
                "</pending_app_category_confirmation>"
            )
            req.extra_user_content_parts.append(
                TextPart(text=text).mark_as_temp()
            )
            logger.info(
                "[scene_aw] pending category confirmation injected:\n%s",
                text,
            )

    def _refresh_category_state_after_assignment(self, app: str) -> None:
        self.scene_detector.reclassify(
            self.category_manager.category_for_app
        )
        new_state = self.category_manager.dashboard_state()
        app_key = self._app_key(app)
        new_state["suggestions"] = [
            item
            for item in self.category_state.get("suggestions", [])
            if self._app_key(str(item.get("app", ""))) != app_key
        ]
        self.category_state = new_state
        self._active_category_confirmations.pop(app_key, None)
        self._new_app_batch.pop(app_key, None)
        focus = self.state.current_focus or {}
        if self._app_key(str(focus.get("app", ""))) == app_key:
            focus["category"] = self.category_manager.category_for_app(app)

    @filter.llm_tool(name="scene_aw_get_current_scene")
    async def scene_aw_get_current_scene(
        self,
        event: AstrMessageEvent,
    ) -> str:
        """查询用户工作电脑当前的实时场景、焦点和状态。"""
        offline = self.state.aw_status == "OFFLINE"
        return json.dumps(
            {
                "aw_status": self.state.aw_status,
                "afk": self.state.afk,
                "primary_scene": None if offline else self.state.primary_scene,
                "scenes": (
                    []
                    if offline
                    else [item.to_dict() for item in self.state.semantic_threads]
                ),
                "current_focus": None if offline else self.state.current_focus,
                "updated_at": self.state.updated_at.isoformat(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @filter.llm_tool(name="scene_aw_confirm_app_category")
    async def scene_aw_confirm_app_category(
        self,
        event: AstrMessageEvent,
        app: str,
        category: str,
    ) -> str:
        """仅当用户明确确认或指定分类时，将已询问的未分类应用写入 ActivityWatch。

        Args:
            app(string): 主动分类提醒中出现的完整应用名称。
            category(string): 用户明确确认或指定的分类名称。
        """
        app = str(app).strip()
        category = str(category).strip()
        key = self._app_key(app)
        if event.unified_msg_origin != self.new_app_notification_umo:
            return "分类失败：当前会话不是新应用提醒的目标会话。"
        if not app:
            return "分类失败：应用名称为空。"
        if category not in self.category_manager.main_categories:
            return "分类失败：分类无效，可用分类为：" + "、".join(
                self.category_manager.main_categories
            )
        if app not in self.category_manager.unknown_apps:
            return "分类失败：该应用当前不是未分类应用。"
        if (
            key not in self._notified_apps
            and key not in self._active_category_confirmations
        ):
            return "分类失败：该应用不在主动分类确认范围内。"
        try:
            await self.category_manager.assign_category(app, category)
            self._refresh_category_state_after_assignment(app)
            logger.info(
                "[scene_aw] category confirmed by agent tool: %s -> %s umo=%s",
                app,
                category,
                event.unified_msg_origin,
            )
            return f"已将 {app} 分类为 {category}。"
        except Exception:
            logger.exception(
                "[scene_aw] agent tool failed to classify app: %s",
                app,
            )
            return f"分类失败：无法写入 {app} 的 ActivityWatch 分类。"

    @filter.llm_tool(name="scene_aw_dismiss_app_category")
    async def scene_aw_dismiss_app_category(
        self,
        event: AstrMessageEvent,
        app: str,
    ) -> str:
        """仅当用户明确拒绝给应用分类时，结束这次对话确认并保留网页待确认项。

        Args:
            app(string): 用户明确拒绝分类的完整应用名称。
        """
        app = str(app).strip()
        key = self._app_key(app)
        if event.unified_msg_origin != self.new_app_notification_umo:
            return "操作失败：当前会话不是新应用提醒的目标会话。"
        if key not in self._active_category_confirmations:
            return "该应用当前没有进行中的分类确认。"
        self._active_category_confirmations.pop(key, None)
        logger.info(
            "[scene_aw] category confirmation dismissed by agent tool: "
            "%s umo=%s",
            app,
            event.unified_msg_origin,
        )
        return f"已结束 {app} 的对话分类确认；它仍保留在网页待确认列表。"

    def _update_focus(
        self,
        events: list[dict[str, Any]],
        now: datetime,
    ) -> bool:
        if not events:
            return False

        event = events[-1]
        payload = event.get("data") or {}

        app = payload.get("app") or "--"

        self.state.current_focus = {
            "app": app,
            "title": payload.get("title") or "--",
            "category": self.category_manager.category_for_app(app),
            "duration_seconds": float(
                event.get("duration") or 0
            ),
            "timestamp": event.get("timestamp"),
        }
        return True

    @staticmethod
    def _latest_event_at(
        data: dict[str, list[dict[str, Any]]],
    ) -> datetime | None:
        latest = None
        for events in data.values():
            for event in events:
                value = event.get("timestamp")
                if not value:
                    continue
                try:
                    at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                    if at.tzinfo is None:
                        at = at.replace(tzinfo=timezone.utc)
                    at += timedelta(seconds=float(event.get("duration") or 0))
                except (TypeError, ValueError):
                    continue
                if latest is None or at > latest:
                    latest = at
        return latest

    def _record_recent_activities(
        self,
        data: dict[str, list[dict[str, Any]]],
    ) -> None:
        existing = {}
        for item in self.recent_activities:
            event_id = item.get("event_id")
            key = (
                ("id", event_id)
                if event_id is not None
                else (
                    "event",
                    item.get("timestamp"),
                    item.get("app"),
                    item.get("title"),
                )
            )
            existing[key] = item

        for event in data.get("window", []):
            payload = event.get("data") or {}
            app = str(payload.get("app") or "--")
            title = str(payload.get("title") or "--")
            timestamp = event.get("timestamp")
            event_id = event.get("id")
            key = (
                ("id", event_id)
                if event_id is not None
                else ("event", timestamp, app, title)
            )

            if not timestamp:
                continue

            current = existing.get(key)
            if current is not None:
                current.update({
                    "timestamp": timestamp,
                    "duration_seconds": max(
                        0.0,
                        float(event.get("duration") or 0),
                    ),
                    "app": app,
                    "title": title,
                    "category": self.category_manager.category_for_app(app),
                })
                continue

            item = {
                "event_id": event_id,
                "timestamp": timestamp,
                "duration_seconds": max(
                    0.0,
                    float(event.get("duration") or 0),
                ),
                "app": app,
                "title": title,
                "category": self.category_manager.category_for_app(app),
            }
            self.recent_activities.append(item)
            existing[key] = item

        self.recent_activities.sort(
            key=lambda item: item.get("timestamp") or ""
        )
        if len(self.recent_activities) > 100:
            self.recent_activities = self.recent_activities[-100:]

    def _aggregated_recent_activities(self) -> list[dict[str, Any]]:
        by_app: dict[str, dict[str, Any]] = {}

        for item in self.recent_activities:
            app = str(item.get("app") or "--")
            key = app.casefold()
            current = by_app.get(key)

            if current is None:
                by_app[key] = {
                    **item,
                    "duration_seconds": float(
                        item.get("duration_seconds") or 0
                    ),
                    "event_count": 1,
                }
                continue

            current["duration_seconds"] += float(
                item.get("duration_seconds") or 0
            )
            current["event_count"] += 1

            if str(item.get("timestamp") or "") >= str(
                current.get("timestamp") or ""
            ):
                current.update({
                    "timestamp": item.get("timestamp"),
                    "title": item.get("title"),
                    "category": item.get("category"),
                })

        return sorted(
            by_app.values(),
            key=lambda item: item.get("timestamp") or "",
            reverse=True,
        )

    def _update_afk(
        self,
        events: list[dict[str, Any]],
    ):
        if not events:
            return

        event = events[-1]
        status = str(
            (event.get("data") or {}).get("status", "")
        ).lower()

        if status == "afk":
            self.state.afk = True
        elif status in {"not-afk", "active"}:
            self.state.afk = False

    def _clear_current_scene_for_afk(self, now: datetime) -> None:
        had_scene = bool(
            self.state.primary_scene or self.state.semantic_threads
        )
        self.state.primary_scene = None
        self.state.semantic_threads = []
        self.state.scene_updated_at = now
        self.scene_detector.candidate_since = None

        if had_scene:
            logger.info("[scene_aw] current scene cleared on AFK")

    async def confirm_category(self):
        payload = await request.json(default={})

        app = str(payload.get("app", "")).strip()
        category = str(payload.get("category", "")).strip()

        if not app:
            return error_response("app is required")

        if category not in self.category_manager.main_categories:
            return error_response("invalid category")

        try:
            await self.category_manager.assign_category(app, category)
            self._refresh_category_state_after_assignment(app)

            logger.info(
                "[scene_aw] category manually confirmed: %s -> %s",
                app,
                category,
            )

            return json_response(
                {
                    "ok": True,
                    "app": app,
                    "category": category,
                }
            )

        except Exception:
            logger.exception(
                "[scene_aw] failed to confirm category: %s",
                app,
            )
            return error_response("failed to update category")

    async def classify_categories(self):
        logger.info(
            "[scene_aw] Category LLM button clicked"
        )
        provider_id = str(
            self.config.get("category_llm_provider_id", "")
        )

        if not provider_id:
            return error_response("未配置 Category LLM Provider")

        if not self.category_llm_prompt:
            logger.error(
                "[scene_aw] Category LLM classification blocked: "
                "category_llm_prompt is empty"
            )
            return error_response("Category LLM 提示词为空")

        if not self.category_state.get("initialized"):
            return error_response("应用分类尚未初始化")

        if self._category_classify_lock.locked():
            return error_response("分类识别正在进行")

        async with self._category_classify_lock:
            samples = self.category_manager.unknown_app_samples()

            logger.info(
                "[scene_aw] Category LLM classification started: "
                "apps=%d provider=%s",
                len(samples),
                provider_id,
            )

            if not samples:
                return json_response({
                    "ok": True,
                    "classified": 0,
                    "applied": 0,
                    "pending": 0,
                    "categories": self.category_state,
                })

            suggestions = []

            try:
                for i in range(0, len(samples), 20):
                    logger.info(
                        "[scene_aw] Category LLM batch: %d-%d/%d",
                        i + 1,
                        min(i + 20, len(samples)),
                        len(samples),
                    )
                    suggestions.extend(
                        await self.category_classifier.classify_batch(
                            samples[i:i + 20]
                        )
                    )

                for item in suggestions:
                    logger.info(
                        "[scene_aw] Category LLM suggestion: "
                        "app=%s category=%s confidence=%.2f",
                        item.get("app", ""),
                        item.get("category", "其他"),
                        float(item.get("confidence", 0) or 0),
                    )

                threshold = float(
                    self.config.get(
                        "category_auto_apply_confidence",
                        0.9,
                    )
                )
                applied = await self.category_manager.apply_suggestions(
                    suggestions,
                    threshold,
                )
                self.category_manager._reclassify_apps()
                self.scene_detector.reclassify(
                    self.category_manager.category_for_app
                )

                new_state = self.category_manager.dashboard_state()
                remaining = {
                    item["app"]
                    for item in new_state["unknown_apps"]
                }
                pending = [
                    item
                    for item in suggestions
                    if item.get("app") in remaining
                ]
                new_state["suggestions"] = pending
                self.category_state = new_state

                logger.info(
                    "[scene_aw] manual category AI finished: "
                    "apps=%d suggested=%d applied=%d pending=%d",
                    len(samples),
                    len(suggestions),
                    applied,
                    len(pending),
                )

                return json_response({
                    "ok": True,
                    "classified": len(samples),
                    "applied": applied,
                    "pending": len(pending),
                    "categories": self.category_state,
                })

            except Exception:
                logger.exception(
                    "[scene_aw] manual category AI classification failed"
                )
                return error_response("分类识别失败")

    async def classify_categories_web(self):
        logger.info(
            "[scene_aw] batch Tavily classification button clicked"
        )
        if not self.category_classifier.provider_id:
            return error_response("未配置 Category LLM Provider")

        if not self.category_llm_prompt:
            logger.error(
                "[scene_aw] Tavily-assisted classification blocked: "
                "category_llm_prompt is empty"
            )
            return error_response("Category LLM 提示词为空")

        if not self.app_web_lookup.api_key:
            return error_response("未配置 Tavily API Key")

        if self._category_classify_lock.locked():
            return error_response("分类识别正在进行")

        async with self._category_classify_lock:
            try:
                current = self.category_state.get("suggestions", [])
                pending_apps = [
                    str(item.get("app", "")).strip()
                    for item in current
                    if str(item.get("app", "")).strip()
                    in self.category_manager.unknown_apps
                ]

                if not pending_apps:
                    return error_response("暂无待确认应用")

                logger.info(
                    "[scene_aw] batch Tavily classification started: "
                    "apps=%d concurrency=3",
                    len(pending_apps),
                )

                semaphore = asyncio.Semaphore(3)

                async def lookup(app: str):
                    async with semaphore:
                        try:
                            evidence = await self.app_web_lookup.search(app)
                        except Exception:
                            logger.warning(
                                "[scene_aw] Tavily lookup failed: %s",
                                app,
                            )
                            return None

                    if not evidence:
                        logger.warning(
                            "[scene_aw] Tavily lookup returned no results: %s",
                            app,
                        )
                        return None

                    logger.info(
                        "[scene_aw] Tavily lookup finished: "
                        "app=%s results=%d",
                        app,
                        len(evidence),
                    )

                    return {
                        "app": app,
                        "titles": self.category_manager.app_titles.get(
                            app,
                            [],
                        )[:5],
                        "web_evidence": evidence,
                    }

                samples = [
                    item
                    for item in await asyncio.gather(*(
                        lookup(app) for app in pending_apps
                    ))
                    if item is not None
                ]

                updated = []
                for i in range(0, len(samples), 20):
                    updated.extend(
                        await self.category_classifier.classify_batch(
                            samples[i:i + 20]
                        )
                    )

                for item in updated:
                    logger.info(
                        "[scene_aw] Tavily-assisted suggestion: "
                        "app=%s category=%s confidence=%.2f",
                        item.get("app", ""),
                        item.get("category", "其他"),
                        float(item.get("confidence", 0) or 0),
                    )

                web_threshold = float(
                    self.config.get(
                        "category_web_auto_apply_confidence",
                        0.5,
                    )
                )
                applied = await self.category_manager.apply_suggestions(
                    updated,
                    web_threshold,
                )
                self.category_manager._reclassify_apps()
                self.scene_detector.reclassify(
                    self.category_manager.category_for_app
                )

                by_app = {
                    str(item.get("app", "")): item
                    for item in current
                }
                for item in updated:
                    by_app[str(item.get("app", ""))] = item

                new_state = self.category_manager.dashboard_state()
                remaining = {
                    item["app"]
                    for item in new_state["unknown_apps"]
                }
                new_state["suggestions"] = [
                    by_app[app]
                    for app in pending_apps
                    if app in remaining and app in by_app
                ]
                self.category_state = new_state

                logger.info(
                    "[scene_aw] batch Tavily classification finished: "
                    "requested=%d searched=%d updated=%d applied=%d "
                    "pending=%d failed=%d threshold=%.2f",
                    len(pending_apps),
                    len(samples),
                    len(updated),
                    applied,
                    len(new_state["suggestions"]),
                    len(pending_apps) - len(samples),
                    web_threshold,
                )

                return json_response({
                    "ok": True,
                    "requested": len(pending_apps),
                    "searched": len(samples),
                    "updated": len(updated),
                    "applied": applied,
                    "pending": len(new_state["suggestions"]),
                    "failed": len(pending_apps) - len(samples),
                    "categories": self.category_state,
                })
            except Exception:
                logger.exception(
                    "[scene_aw] batch web category classification failed"
                )
                return error_response("批量联网识别失败")

    async def _maybe_interpret_scene(
        self,
        force_reason: str | None = None,
    ):
        provider_id = str(
            self.config.get(
                "scene_llm_provider_id",
                "",
            )
        )

        if not provider_id:
            return False

        if not self.scene_llm_prompt:
            return False

        if self.state.aw_status == "OFFLINE":
            return False

        clusters = self.scene_detector.activity_clusters()
        recent_activity = self.scene_detector.activity_segments()
        enhanced_events = sorted(
            self.enhanced_context_events.values(),
            key=lambda item: item.get("timestamp", ""),
        )

        if force_reason:
            # 第一个完整快照才消耗启动强制识别。
            if (
                self.state.afk
                or not self.category_state.get("initialized")
                or not self.state.current_focus
                or not self.state.category_profile
                or not clusters
            ):
                return False

            should_run = True
            reason = force_reason

        else:
            should_run, reason = (
                self.scene_detector.should_interpret(
                    has_scene=bool(self.state.semantic_threads),
                    afk=self.state.afk,
                    enhanced_events=enhanced_events,
                )
            )

        if not should_run or not reason:
            return False

        try:
            result, debug = (
                await self.scene_interpreter.interpret(
                    category_summary=clusters,
                    recent_activity=recent_activity,
                    focus=self.state.current_focus,
                    communication=self.communication_state,
                    enhanced_events=enhanced_events,
                    reason=reason,
                )
            )

            now = datetime.now(
                timezone.utc
            )

            self.last_llm = {
                "at": now.isoformat(),
                **debug,
            }

            self.scene_detector.mark_interpreted(
                enhanced_events=enhanced_events,
            )

            if result is None:
                return True

            # 归一 LLM 对同一事务产生的轻微名称变化，例如
            # “开发 Scene Awareness 插件”与“Scene Awareness 插件开发”。
            similarity_threshold = max(
                0.0,
                min(
                    1.0,
                    float(
                        self.config.get(
                            "scene_name_similarity_threshold",
                            0.82,
                        )
                    ),
                ),
            )
            previous_names = [
                thread.name
                for thread in self.state.semantic_threads
                if thread.name
            ]
            claimed_names: set[str] = set()
            for item in result["scenes"]:
                name = str(item.get("name", "")).strip()
                best_name = None
                best_ratio = 0.0
                for previous_name in previous_names:
                    if previous_name in claimed_names:
                        continue
                    ratio = SequenceMatcher(
                        None,
                        name.casefold(),
                        previous_name.casefold(),
                    ).ratio()
                    if ratio > best_ratio:
                        best_name = previous_name
                        best_ratio = ratio
                if best_name and best_ratio >= similarity_threshold:
                    item["name"] = best_name
                    claimed_names.add(best_name)

            old_threads = {
                thread.name: thread.to_dict()
                for thread in self.state.semantic_threads
                if thread.name
            }

            new_threads: list[SemanticThread] = []

            for item in result["scenes"]:
                old = old_threads.get(
                    item["name"]
                )

                started_at = now
                if old and old.get("started_at"):
                    started_at = datetime.fromisoformat(old["started_at"])
                new_threads.append(SemanticThread(
                    **item,
                    started_at=started_at,
                    last_active_at=now,
                ))

            old_primary = next(
                (
                    item
                    for item
                    in self.state.semantic_threads
                    if item.role
                    == "primary"
                ),
                None,
            )

            new_primary = next(
                (
                    item
                    for item
                    in new_threads
                    if item.role
                    == "primary"
                ),
                None,
            )

            min_switch_confidence = float(
                self.config.get("scene_switch_min_confidence", 0.65)
            )
            if (
                old_primary
                and new_primary
                and old_primary.name != new_primary.name
                and new_primary.confidence < min_switch_confidence
            ):
                for thread in new_threads:
                    thread.role = "secondary"

                retained = next(
                    (
                        thread for thread in new_threads
                        if thread.name == old_primary.name
                    ),
                    None,
                )
                if retained:
                    retained.role = "primary"
                    new_primary = retained
                else:
                    retained = SemanticThread(
                        name=old_primary.name,
                        role="primary",
                        confidence=old_primary.confidence,
                        started_at=old_primary.started_at,
                        last_active_at=old_primary.last_active_at,
                    )
                    new_threads.insert(0, retained)
                    new_threads = new_threads[
                        :self.scene_interpreter.max_scenes
                    ]
                    new_primary = retained

            if (
                old_primary
                and new_primary
                and old_primary.name
                != new_primary.name
            ):
                self.previous_scene = {
                    **old_primary.to_dict(),
                    "ended_at": now.isoformat(),
                }

            self.state.semantic_threads = new_threads
            self.state.primary_scene = new_primary.name if new_primary else None
            self.state.scene_updated_at = now

            if new_primary:
                logger.info(
                    "[scene_aw] scene interpreted: "
                    "primary=%s threads=%d",
                    new_primary.name,
                    len(self.state.semantic_threads),
                )

            return True

        except Exception:
            logger.exception(
                "[scene_aw] scene interpretation failed"
            )
            return True

    async def dashboard_state(self):
        # 即使某些环境下 __init__ 没拿到 running loop，
        # 打开页面时也能确保扫描器启动。
        await self._ensure_runtime_started()

        now = datetime.now(timezone.utc)
        raw = self.state.to_dict()

        threads = raw.get("semantic_threads", [])
        current_focus = raw.get("current_focus")

        primary_scene = raw.get("primary_scene")

        return json_response(
            {
                "health": {
                    "running": (
                        self._scan_task is not None
                        and not self._scan_task.done()
                    ),
                    "aw_connected": self.aw_connected,
                    "aw_status": raw.get("aw_status", "OFFLINE"),
                    "bucket_count": self.bucket_count,
                    "last_scan_at": raw.get("last_scan_at"),
                    "scan_interval_seconds": self.scan_interval,
                    "uptime_seconds": (
                        now - self.started_at
                    ).total_seconds(),
                },

                "scene": {
                    "primary_scene": primary_scene,
                    "threads": threads,
                    "category_profile": raw.get("category_profile", {}),
                    "scene_updated_at": raw.get("scene_updated_at"),
                    "profile_window_seconds": int(
                        self.config.get(
                            "scene_profile_window_seconds",
                            300,
                        )
                    ),
                    "current_focus": current_focus,
                    "communication": self.communication_state,
                    "signals": {
                        "afk": raw.get("afk", False),
                    },
                },

                "previous_scene": self.previous_scene,
                "last_llm": self.last_llm,
                "categories": self.category_state,
                "recent_activities": self._aggregated_recent_activities()[:30],
            }
        )

    async def terminate(self):
        self._stopping = True

        if self._scan_task:
            self._scan_task.cancel()

            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass

        logger.info("[scene_aw] plugin stopped")
