"""适配器导出。

当前链路（2026-11-12 起）：
  - GrowthBrowser      : CDP 直连成长中心 iframe，负责签到状态校验与礼物/派遣/兑换
  - AgentDesktopSigner : 可选 AX 兜底（agent-desktop，严格 headless）

旧的 CuaDriver 通道（CuaDriverSigner / GrowthCenter）已整体移除，
不会再被任何代码路径引用。
"""
from .base import ActionResult, DesktopAdapter
from .desktop_agent import AgentDesktopSigner
from .growth_browser import GrowthBrowser

__all__ = [
    "ActionResult",
    "DesktopAdapter",
    "AgentDesktopSigner",
    "GrowthBrowser",
]
