from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class SemanticThread:
    name: str
    role: str
    confidence: float = 0
    started_at: datetime | None = None
    last_active_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "confidence": self.confidence,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_active_at": self.last_active_at.isoformat() if self.last_active_at else None,
        }


@dataclass
class SceneState:
    updated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    current_focus: dict[str, Any] | None = None
    primary_scene: str | None = None
    semantic_threads: list[SemanticThread] = field(default_factory=list)
    category_profile: dict[str, float] = field(default_factory=dict)
    scene_updated_at: datetime | None = None
    aw_status: str = "OFFLINE"
    communication_active_seconds: float = 0
    afk: bool = False
    last_scan_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "updated_at": self.updated_at.isoformat(),
            "current_focus": self.current_focus,
            "primary_scene": self.primary_scene,
            "semantic_threads": [t.to_dict() for t in self.semantic_threads],
            "category_profile": dict(self.category_profile),
            "scene_updated_at": self.scene_updated_at.isoformat() if self.scene_updated_at else None,
            "aw_status": self.aw_status,
            "communication_active_seconds": self.communication_active_seconds,
            "afk": self.afk,
            "last_scan_at": (
                self.last_scan_at.isoformat()
                if self.last_scan_at else None
            ),
        }
