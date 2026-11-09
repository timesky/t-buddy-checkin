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


class WebAdapter:
    """网页端，headless 独立会话。"""

    def open(self, url: str | None = None) -> ActionResult:
        raise NotImplementedError

    def snapshot(self) -> str:
        raise NotImplementedError

    def eval_js(self, js: str) -> str:
        raise NotImplementedError

    def click_text(self, texts: list[str]) -> bool:
        raise NotImplementedError

    def get_url(self) -> str:
        raise NotImplementedError

    def screenshot(self, path: str) -> str:
        raise NotImplementedError

    def wait(self, ms: int = 3000) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class DesktopAdapter:
    """桌面端，仅用于每日签到；默认后台执行，不激活窗口、不移动鼠标。"""

    def probe(self) -> ActionResult:
        raise NotImplementedError

    def claim_daily(self, texts: list[str]) -> ActionResult:
        raise NotImplementedError
