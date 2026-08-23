from __future__ import annotations

import json
import re
import time
from typing import Any


class SceneInterpreter:
    def __init__(
        self,
        context,
        provider_id: str,
        max_scenes: int = 3,
        prompt_template: str = "",
    ):
        self.context = context
        self.provider_id = provider_id
        self.max_scenes = max(1, min(3, int(max_scenes)))
        self.prompt_template = str(prompt_template or "").strip()

    async def interpret(
        self,
        *,
        category_summary: list[dict[str, Any]],
        recent_activity: list[dict[str, Any]],
        focus: dict[str, Any] | None,
        communication: dict[str, Any],
        enhanced_events: list[dict[str, Any]],
        reason: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        if not self.provider_id or not self.prompt_template:
            return None, {}

        enrichment: dict[tuple[str, str], dict[str, Any]] = {}

        for event in enhanced_events:
            data = event.get("data") or {}
            app = str(data.get("app") or "").strip()
            if not app:
                continue

            title = str(data.get("title") or "").strip()
            key = (app.casefold(), title.casefold().strip())
            entry = enrichment.setdefault(key, {
                "ocr_keywords": [],
                "ocr_seen": set(),
            })

            keywords = data.get("ocr_keywords")
            if not isinstance(keywords, list):
                continue

            for value in keywords:
                keyword = str(value).strip()
                if not keyword:
                    continue
                normalized = keyword.casefold()
                if normalized not in entry["ocr_seen"]:
                    entry["ocr_seen"].add(normalized)
                    entry["ocr_keywords"].append(keyword)

        compact_categories = [
            {
                "category": item.get("category"),
                "active_seconds": round(
                    float(item.get("active_seconds") or 0)
                ),
            }
            for item in category_summary[:6]
        ]

        compact_activity = []
        for item in recent_activity[-8:]:
            app = str(item.get("app") or "").strip()
            title = str(item.get("title") or "").strip()
            if not app:
                continue
            segment = {
                "app": app,
                "category": item.get("category"),
                "title": title,
                "active_seconds": round(
                    float(item.get("active_seconds") or 0)
                ),
            }
            enriched = enrichment.get(
                (app.casefold(), title.casefold().strip()),
                {},
            )
            if enriched.get("ocr_keywords"):
                segment["ocr_keywords"] = enriched["ocr_keywords"][:20]
            compact_activity.append(segment)

        compact_focus = None

        if focus:
            compact_focus = {
                "app": focus.get("app"),
                "title": focus.get("title"),
                "category": focus.get("category"),
                "duration_seconds": round(
                    float(focus.get("duration_seconds") or 0)
                ),
            }
            if focus.get("enhanced"):
                compact_focus["enhanced"] = focus["enhanced"]

        payload = {
            "category_summary": compact_categories,
            "recent_activity": compact_activity,
            "current_focus": compact_focus,
            "communication": {
                "active": communication.get("active", False),
                "active_seconds": round(
                    float(
                        communication.get(
                            "active_seconds",
                            0,
                        )
                        or 0
                    )
                ),
                "apps": communication.get("apps", [])[:6],
            },
        }

        prompt = self.prompt_template.replace(
            "{{max_secondary}}",
            str(max(0, self.max_scenes - 1)),
        )
        encoded_input = json.dumps(payload, ensure_ascii=False)
        if "{{input}}" in prompt:
            prompt = prompt.replace("{{input}}", encoded_input)
        else:
            # 自定义模板漏写占位符时仍确保模型能收到输入。
            prompt = f"{prompt}\n\n输入：\n{encoded_input}"

        started = time.monotonic()

        resp = await self.context.llm_generate(
            chat_provider_id=self.provider_id,
            prompt=prompt,
        )

        latency_ms = round(
            (time.monotonic() - started) * 1000
        )

        usage = getattr(resp, "usage", None)
        input_tokens = None
        input_cached_tokens = None
        output_tokens = None

        if usage is not None:
            input_other = int(getattr(usage, "input_other", 0) or 0)
            input_cached_tokens = int(
                getattr(usage, "input_cached", 0) or 0
            )
            input_tokens = input_other + input_cached_tokens
            output_tokens = int(getattr(usage, "output", 0) or 0)

        text = resp.completion_text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

        result = None
        json_repaired = False

        def repair_confidence_decimal(value: str) -> str:
            # 只修复 confidence 中明确的 0-81 -> 0.81 类型错误。
            return re.sub(
                r'("confidence"\s*:\s*)0-(\d+)',
                r'\g<1>0.\2',
                value,
            )

        try:
            decoded = json.loads(text)

            # 某些 Provider 会把 JSON 结果再次编码成字符串。
            if isinstance(decoded, str):
                try:
                    result = json.loads(decoded)
                except json.JSONDecodeError:
                    repaired = repair_confidence_decimal(decoded)
                    if repaired != decoded:
                        result = json.loads(repaired)
                        json_repaired = True
            else:
                result = decoded

        except json.JSONDecodeError:
            repaired = repair_confidence_decimal(text)
            if repaired != text:
                try:
                    result = json.loads(repaired)
                    json_repaired = True
                except json.JSONDecodeError:
                    result = None

        debug = {
            "reason": reason,
            "latency_ms": latency_ms,
            "input_tokens": input_tokens,
            "input_cached_tokens": input_cached_tokens,
            "output_tokens": output_tokens,
            "input_summary": payload,
            "output": result or {"raw": text},
        }

        if json_repaired:
            debug["json_repaired"] = True

        if not isinstance(result, dict):
            return None, debug

        raw_scenes = result.get("scenes")

        if not isinstance(raw_scenes, list):
            return None, debug

        scenes = []
        seen_names = set()

        for item in raw_scenes:
            if len(scenes) >= self.max_scenes:
                break

            if not isinstance(item, dict):
                continue

            name = str(item.get("name", "")).strip()

            if not name:
                continue

            normalized_name = name.casefold()
            if normalized_name in seen_names:
                continue
            seen_names.add(normalized_name)

            role = str(
                item.get("role", "secondary")
            ).strip()

            if role not in {
                "primary",
                "secondary",
            }:
                role = "secondary"

            try:
                confidence = float(
                    item.get("confidence", 0)
                )
            except Exception:
                confidence = 0.0

            scenes.append({
                "name": name,
                "role": role,
                "confidence": max(
                    0.0,
                    min(1.0, confidence),
                ),
            })

        if not scenes:
            focus_category = str((focus or {}).get("category") or "")
            focus_title = str((focus or {}).get("title") or "").strip()

            if focus_category.split(" > ", 1)[0] == "游戏" and focus_title:
                scenes = [{
                    "name": f"游玩《{focus_title}》",
                    "role": "primary",
                    "confidence": 0.75,
                }]
                debug["fallback"] = "empty_game_scene"
            else:
                return None, debug

        # 强制最多一个 primary。
        primary_found = False

        for item in scenes:
            if (
                item["role"] == "primary"
                and not primary_found
            ):
                primary_found = True
            else:
                item["role"] = "secondary"

        if not primary_found:
            scenes[0]["role"] = "primary"

        scenes.sort(
            key=lambda x: (
                x["role"] != "primary",
                -x["confidence"],
            )
        )

        normalized_result = {
            "scenes": scenes,
        }
        debug["output"] = normalized_result
        return normalized_result, debug
