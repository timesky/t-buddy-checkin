"""适配器接口约定。业务层只依赖这些抽象，便于用假适配器做全链路单测。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ActionResult:
    ok: bool
    action: str
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "detail": self.detail,
            "data": self.data,
        }


class DesktopAdapter:
    """桌面端，仅用于每日签到；后台执行，不激活窗口、不移动鼠标。"""

    def probe(self) -> ActionResult:
        raise NotImplementedError

    def claim_daily(self, texts: list[str]) -> ActionResult:
        raise NotImplementedError
