from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Callable


COMMUNICATION_CATEGORIES = {"通讯"}


class SceneDetector:
    def __init__(self, hold_seconds: int = 180,
                 llm_min_interval_seconds: int = 300,
                 profile_window_seconds: int = 300,
                 profile_change_threshold: float = 0.35):
        self.hold_seconds = hold_seconds
        self.llm_min_interval_seconds = llm_min_interval_seconds
        self.profile_window_seconds = profile_window_seconds
        self.profile_change_threshold = profile_change_threshold
        self.samples: deque[dict[str, Any]] = deque()
        self.afk_intervals: deque[tuple[datetime, datetime]] = deque()
        self._sample_keys: set[tuple[Any, ...]] = set()
        self._afk_keys: set[tuple[Any, ...]] = set()
        self._samples_by_source: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._afk_by_source: dict[
            tuple[Any, ...], tuple[datetime, datetime]
        ] = {}
        self.reference_profile: dict[str, dict[str, float]] | None = None
        self.candidate_since: datetime | None = None
        self.last_llm_at: datetime | None = None

    @staticmethod
    def _event_interval(event: dict[str, Any]) -> tuple[datetime, datetime] | None:
        value = event.get("timestamp")
        if not value:
            return None
        try:
            start = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            duration = max(0.0, float(event.get("duration") or 0))
        except (TypeError, ValueError):
            return None
        return start, start + timedelta(seconds=duration)

    def add_events(self, *, window_events: list[dict[str, Any]],
                   afk_events: list[dict[str, Any]],
                   category_for_app: Callable[[str], str | None],
                   now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        for event in afk_events:
            status = str((event.get("data") or {}).get("status", "")).lower()
            if status != "afk":
                continue
            interval = self._event_interval(event)
            if interval is None:
                continue
            source_key = (
                ("id", event.get("id"))
                if event.get("id") is not None
                else ("event", interval[0], "afk")
            )
            previous = self._afk_by_source.get(source_key)
            if previous == interval:
                continue
            if previous is not None:
                try:
                    self.afk_intervals.remove(previous)
                except ValueError:
                    pass
                self._afk_keys.discard(previous)
            self._afk_keys.add(interval)
            self.afk_intervals.append(interval)
            self._afk_by_source[source_key] = interval

        for event in window_events:
            interval = self._event_interval(event)
            if interval is None:
                continue
            payload = event.get("data") or {}
            app = str(payload.get("app") or "--")
            title = str(payload.get("title") or "--")
            key = (interval[0], interval[1], app, title)
            source_key = (
                ("id", event.get("id"))
                if event.get("id") is not None
                else ("event", interval[0], app, title)
            )
            previous = self._samples_by_source.get(source_key)
            if previous and previous["key"] == key:
                continue
            if previous is not None:
                try:
                    self.samples.remove(previous)
                except ValueError:
                    pass
                self._sample_keys.discard(previous["key"])
            self._sample_keys.add(key)
            sample = {
                "start": interval[0], "end": interval[1], "app": app,
                "title": title,
                "category": category_for_app(app) or "其他", "key": key,
                "source_key": source_key,
            }
            self.samples.append(sample)
            self._samples_by_source[source_key] = sample
        self._prune(now)

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.profile_window_seconds)
        while self.samples and self.samples[0]["end"] < cutoff:
            sample = self.samples.popleft()
            self._sample_keys.discard(sample["key"])
            if self._samples_by_source.get(sample["source_key"]) is sample:
                self._samples_by_source.pop(sample["source_key"], None)
        while self.afk_intervals and self.afk_intervals[0][1] < cutoff:
            interval = self.afk_intervals.popleft()
            self._afk_keys.discard(interval)
            for key, value in list(self._afk_by_source.items()):
                if value == interval:
                    self._afk_by_source.pop(key, None)

    def reclassify(
        self,
        category_for_app: Callable[[str], str | None],
    ) -> None:
        for sample in self.samples:
            sample["category"] = category_for_app(sample["app"]) or "其他"

    def _active_seconds(self, sample: dict[str, Any],
                        start: datetime, end: datetime) -> float:
        overlap_start = max(sample["start"], start)
        overlap_end = min(sample["end"], end)
        seconds = max(0.0, (overlap_end - overlap_start).total_seconds())
        for afk_start, afk_end in self.afk_intervals:
            excluded_start = max(overlap_start, afk_start)
            excluded_end = min(overlap_end, afk_end)
            seconds -= max(0.0, (excluded_end - excluded_start).total_seconds())
        return max(0.0, seconds)

    def activity_clusters(self, now: datetime | None = None, *,
                          include_communication: bool = False) -> list[dict[str, Any]]:
        now = now or datetime.now(timezone.utc)
        self._prune(now)
        start = now - timedelta(seconds=self.profile_window_seconds)
        grouped: dict[str, dict[str, Any]] = {}
        for sample in self.samples:
            category = sample["category"]
            if (not include_communication
                    and category.split(" > ", 1)[0] in COMMUNICATION_CATEGORIES):
                continue
            seconds = self._active_seconds(sample, start, now)
            if seconds <= 0:
                continue
            item = grouped.setdefault(category, {
                "category": category,
                "active_seconds": 0.0,
                "app_seconds": {},
            })
            item["active_seconds"] += seconds
            item["app_seconds"][sample["app"]] = (
                item["app_seconds"].get(sample["app"], 0.0)
                + seconds
            )
        result = [{
            "category": item["category"],
            "active_seconds": round(item["active_seconds"], 2),
            "apps": sorted(item["app_seconds"]),
            "applications": [
                {
                    "app": app,
                    "active_seconds": round(seconds, 2),
                }
                for app, seconds in sorted(
                    item["app_seconds"].items(),
                    key=lambda pair: pair[1],
                    reverse=True,
                )
            ],
        } for item in grouped.values()]
        result.sort(key=lambda item: item["active_seconds"], reverse=True)
        return result

    @staticmethod
    def _normalized_title(title: str, app: str = "") -> str:
        value = str(title or "").casefold().strip()
        value = re.sub(r"和另外\s*\d+\s*个页面", "", value)
        value = re.sub(
            r"\s+-\s+(?:microsoft.? edge|visual studio code|sublime text.*)$",
            "",
            value,
        )
        if str(app).casefold() in {
            "code.exe",
            "cursor.exe",
            "vscodium.exe",
        }:
            parts = value.split(" - ", 1)
            if len(parts) == 2:
                value = parts[1]
            value = re.sub(r"\s*\[ssh:[^\]]+\]\s*$", "", value)
        value = re.sub(r"\s+", " ", value)
        return value.strip(" -—_")

    @staticmethod
    def _enhanced_keywords(
        enhanced_events: list[dict[str, Any]],
    ) -> dict[tuple[str, str], list[str]]:
        grouped: dict[tuple[str, str], list[str]] = {}
        seen: dict[tuple[str, str], set[str]] = {}
        generic = {
            "app", "data", "edit", "file", "help", "open", "run",
            "text", "title", "window", "exe", "untitled",
        }

        for event in enhanced_events:
            data = event.get("data") or {}
            app = str(data.get("app") or "").casefold()
            title = SceneDetector._normalized_title(
                data.get("title") or "",
                app,
            )
            keywords = data.get("ocr_keywords")
            if not app or not isinstance(keywords, list):
                continue
            key = (app, title)
            values = grouped.setdefault(key, [])
            normalized_seen = seen.setdefault(key, set())
            for raw_keyword in keywords:
                keyword = str(raw_keyword).casefold().strip()
                keyword = re.sub(r"\s+", " ", keyword)
                if (
                    not keyword
                    or keyword in generic
                    or len(keyword) < 3
                    or keyword in normalized_seen
                ):
                    continue
                normalized_seen.add(keyword)
                values.append(keyword)

        return grouped

    def activity_segments(
        self,
        now: datetime | None = None,
        *,
        max_segments: int = 8,
        merge_gap_seconds: int = 45,
    ) -> list[dict[str, Any]]:
        now = now or datetime.now(timezone.utc)
        self._prune(now)
        start = now - timedelta(seconds=self.profile_window_seconds)
        segments: list[dict[str, Any]] = []

        for sample in sorted(self.samples, key=lambda item: item["start"]):
            seconds = self._active_seconds(sample, start, now)
            if seconds <= 0:
                continue
            segment_start = max(sample["start"], start)
            segment_end = min(sample["end"], now)
            current = {
                "app": sample["app"],
                "category": sample["category"],
                "title": sample.get("title") or "--",
                "active_seconds": round(seconds, 2),
                "started_at": segment_start.isoformat(),
                "ended_at": segment_end.isoformat(),
            }

            previous = segments[-1] if segments else None
            if previous:
                previous_end = datetime.fromisoformat(previous["ended_at"])
                gap = (segment_start - previous_end).total_seconds()
                same_activity = (
                    previous["app"].casefold() == current["app"].casefold()
                    and previous["category"] == current["category"]
                    and self._normalized_title(
                        previous["title"], previous["app"]
                    )
                    == self._normalized_title(
                        current["title"], current["app"]
                    )
                )
                if same_activity and gap <= merge_gap_seconds:
                    previous["active_seconds"] = round(
                        previous["active_seconds"] + current["active_seconds"],
                        2,
                    )
                    previous["ended_at"] = current["ended_at"]
                    continue

            segments.append(current)

        return segments[-max(1, max_segments):]

    @staticmethod
    def _normalize_distribution(values: dict[str, float]) -> dict[str, float]:
        total = sum(values.values())
        if total <= 0:
            return {}
        return {key: value / total for key, value in values.items()}

    def evidence_profile(
        self,
        now: datetime | None = None,
        *,
        enhanced_events: list[dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, float]]:
        now = now or datetime.now(timezone.utc)
        self._prune(now)
        detection_seconds = min(
            self.profile_window_seconds,
            max(90, self.hold_seconds),
        )
        start = now - timedelta(seconds=detection_seconds)
        keyword_map = self._enhanced_keywords(enhanced_events or [])
        categories: dict[str, float] = {}
        apps: dict[str, float] = {}
        semantics: dict[str, float] = {}

        for sample in self.samples:
            seconds = self._active_seconds(sample, start, now)
            if seconds <= 0:
                continue
            category = str(sample.get("category") or "其他")
            app = str(sample.get("app") or "--").casefold()
            title = self._normalized_title(
                sample.get("title") or "",
                app,
            )
            categories[category] = categories.get(category, 0.0) + seconds
            apps[app] = apps.get(app, 0.0) + seconds

            keywords = keyword_map.get((app, title), [])[:10]
            if title:
                title_weight = seconds * (0.7 if keywords else 1.0)
                key = f"title:{title}"
                semantics[key] = semantics.get(key, 0.0) + title_weight
            if keywords:
                keyword_weight = seconds * 0.3 / len(keywords)
                for keyword in keywords:
                    key = f"ocr:{keyword}"
                    semantics[key] = semantics.get(key, 0.0) + keyword_weight

        return {
            "category": self._normalize_distribution(categories),
            "app": self._normalize_distribution(apps),
            "semantic": self._normalize_distribution(semantics),
        }

    @classmethod
    def _evidence_distance(
        cls,
        previous: dict[str, dict[str, float]],
        current: dict[str, dict[str, float]],
    ) -> float:
        return (
            0.25 * cls._profile_distance(
                previous.get("category", {}), current.get("category", {})
            )
            + 0.30 * cls._profile_distance(
                previous.get("app", {}), current.get("app", {})
            )
            + 0.45 * cls._profile_distance(
                previous.get("semantic", {}), current.get("semantic", {})
            )
        )

    def _latest_activity_started_at(self, now: datetime) -> datetime:
        active_samples = [
            sample
            for sample in self.samples
            if sample["start"] <= now and sample["end"] >= now - timedelta(
                seconds=max(60, self.hold_seconds)
            )
        ]
        if not active_samples:
            return now
        latest = max(active_samples, key=lambda item: item["end"])
        return min(now, latest["start"])

    def current_profile(self, now: datetime | None = None) -> dict[str, float]:
        clusters = self.activity_clusters(now, include_communication=True)
        total = sum(item["active_seconds"] for item in clusters)
        if total <= 0:
            return {}
        return {item["category"]: item["active_seconds"] / total for item in clusters}

    def communication_summary(self, now: datetime | None = None,
                              active_within_seconds: int = 60) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        start = now - timedelta(seconds=self.profile_window_seconds)
        active_cutoff = now - timedelta(seconds=active_within_seconds)
        seconds, apps, latest_end = 0.0, set(), None
        for sample in self.samples:
            if sample["category"].split(" > ", 1)[0] not in COMMUNICATION_CATEGORIES:
                continue
            duration = self._active_seconds(sample, start, now)
            if duration <= 0:
                continue
            seconds += duration
            apps.add(sample["app"])
            if latest_end is None or sample["end"] > latest_end:
                latest_end = sample["end"]
        return {
            "active": bool(latest_end and latest_end >= active_cutoff),
            "active_seconds": round(seconds, 2), "apps": sorted(apps),
        }

    @staticmethod
    def _profile_distance(a: dict[str, float], b: dict[str, float]) -> float:
        return sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in set(a) | set(b)) / 2

    def should_interpret(
        self,
        *,
        has_scene: bool,
        afk: bool,
        enhanced_events: list[dict[str, Any]] | None = None,
    ) -> tuple[bool, str | None]:
        now = datetime.now(timezone.utc)
        if afk:
            self.candidate_since = None
            return False, None
        profile = self.evidence_profile(
            now,
            enhanced_events=enhanced_events,
        )
        if not any(profile.values()):
            return False, None
        if not has_scene:
            if self.candidate_since is None:
                self.candidate_since = now
                return False, None
            if ((now - self.candidate_since).total_seconds() >= self.hold_seconds
                    and self._cooldown_ready(now)):
                return True, "initial_scene"
            return False, None
        if self.reference_profile is None:
            self.reference_profile = profile
            self.candidate_since = None
            return False, None
        distance = self._evidence_distance(self.reference_profile, profile)
        if distance < self.profile_change_threshold:
            self.candidate_since = None
            return False, None
        if self.candidate_since is None:
            self.candidate_since = self._latest_activity_started_at(now)
        if (now - self.candidate_since).total_seconds() < self.hold_seconds:
            return False, None
        if not self._cooldown_ready(now):
            return False, None
        return True, "activity_evidence_changed"

    def mark_interpreted(
        self,
        *,
        enhanced_events: list[dict[str, Any]] | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.last_llm_at = now
        self.candidate_since = None
        profile = self.evidence_profile(
            now,
            enhanced_events=enhanced_events,
        )
        if any(profile.values()):
            self.reference_profile = profile

    def _cooldown_ready(self, now: datetime) -> bool:
        return self.last_llm_at is None or (
            now - self.last_llm_at
        ).total_seconds() >= self.llm_min_interval_seconds
