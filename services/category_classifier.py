from __future__ import annotations

import json
import re
from typing import Any


DEFAULT_CATEGORY_DEFINITIONS = {
    "编程": "IDE、代码编辑器、开发/调试工具",
    "运维": "终端、SSH、远程桌面、服务器/环境管理",
    "通讯": "IM、邮件、会议、人与人通信客户端",
    "浏览": "网页浏览器",
    "文档": "笔记、Office、PDF、知识/文档工具",
    "影音": "本地播放器、专用影音客户端",
    "创作": "图像设计、绘画、视频剪辑、音频制作、3D/CAD、直播录制等内容生产工具",
    "游戏": "游戏本体、明确游戏平台/启动器",
    "其他": "系统工具、截图、文件管理、VPN、无法明确归入以上类别",
}

ALLOWED = set(DEFAULT_CATEGORY_DEFINITIONS)


def parse_category_definitions(value: str) -> dict[str, str]:
    definitions: dict[str, str] = {}
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        separator = "：" if "：" in line else ":"
        if separator not in line:
            continue
        name, description = line.split(separator, 1)
        name = name.strip()
        description = description.strip()
        if not name or not description or ">" in name:
            continue
        definitions[name] = description

    if not definitions:
        return dict(DEFAULT_CATEGORY_DEFINITIONS)
    if "其他" not in definitions:
        definitions["其他"] = DEFAULT_CATEGORY_DEFINITIONS["其他"]
    return definitions


class CategoryClassifier:
    def __init__(
        self,
        context,
        provider_id: str,
        prompt_template: str = "",
        category_definitions: dict[str, str] | None = None,
    ):
        self.context = context
        self.provider_id = provider_id
        self.prompt_template = str(prompt_template or "").strip()
        self.category_definitions = (
            dict(category_definitions)
            if category_definitions
            else dict(DEFAULT_CATEGORY_DEFINITIONS)
        )
        self.allowed = set(self.category_definitions)

    async def classify_batch(
        self,
        apps: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not self.provider_id or not self.prompt_template or not apps:
            return []

        payload = []

        for item in apps:
            compact = {
                "app": item["app"],
                "titles": item.get("titles", [])[:5],
            }
            if item.get("web_evidence"):
                compact["web_evidence"] = item["web_evidence"][:5]
            payload.append(compact)

        # 每个提交的应用都必须有结果；模型漏答或输出无效时，
        # 用“其他 / 0”进入待确认，而不是静默消失。
        fallback = {
            item["app"]: {
                "app": item["app"],
                "category": "其他",
                "confidence": 0.0,
            }
            for item in payload
        }

        encoded_input = json.dumps(payload, ensure_ascii=False)
        category_text = "\n".join(
            f"- {name}：{description}"
            for name, description in self.category_definitions.items()
        )
        prompt = self.prompt_template
        if "{{categories}}" in prompt:
            prompt = prompt.replace("{{categories}}", category_text)
        else:
            prompt = (
                f"{prompt}\n\n当前唯一允许的分类：\n{category_text}"
            )
        if "{{input}}" in prompt:
            prompt = prompt.replace("{{input}}", encoded_input)
        else:
            # 自定义模板漏写占位符时仍确保模型收到应用列表。
            prompt = f"{prompt}\n\n待分类应用：\n{encoded_input}"

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=self.provider_id,
                prompt=prompt,
            )
        except Exception:
            return list(fallback.values())

        text = resp.completion_text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

        try:
            result = json.loads(text)
        except Exception:
            return list(fallback.values())

        if not isinstance(result, list):
            return list(fallback.values())

        valid_by_app = dict(fallback)

        requested = {x["app"] for x in apps}

        for item in result:
            if not isinstance(item, dict):
                continue

            app = str(item.get("app", "")).strip()
            category = str(item.get("category", "")).strip()

            try:
                confidence = float(item.get("confidence", 0))
            except Exception:
                confidence = 0.0

            if app not in requested:
                continue
            if category not in self.allowed:
                continue

            valid_by_app[app] = {
                "app": app,
                "category": category,
                "confidence": max(0.0, min(1.0, confidence)),
            }

        return [
            valid_by_app[item["app"]]
            for item in payload
        ]
