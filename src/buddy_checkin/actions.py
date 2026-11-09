"""业务编排：签到 -> 礼物兑换 -> 派遣 -> 精确到期领礼物 -> 再次派遣。

所有领取都经 State.claim_once 幂等。两条通道都走 **CuaDriver**（trycua AX 驱动）：
  - 每日签到    : 桌面端 CuaDriverSigner 操作 WorkBuddy 客户端窗口
  - 礼物/派遣/兑换 : 成长中心 GrowthCenter 操作已登录的个人中心浏览器窗口

不依赖任何 headless 浏览器 CLI（agent-browser 等已移除）；浏览器由用户从 WorkBuddy
点开，未登录时只提示登录、绝不自动登录。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from .adapters.base import ActionResult
from .timeutil import day_key

logger = logging.getLogger(__name__)


class Actions:
    def __init__(self, cfg: dict, state: Any, desktop: Any, growth: Any = None) -> None:
        self.cfg = cfg
        self.state = state
        self.desktop = desktop
        # growth 不为空时，礼物/派遣走 CuaDriver 的成长中心 AX 通道
        self.growth = growth

    # ---- 1. 每日签到（唯一依赖桌面客户端） ---------------------------------
    def daily_checkin(self) -> ActionResult:
        if not self.cfg["daily_checkin"]["enabled"]:
            return ActionResult(True, "checkin", "disabled")
        if self.state.kv_get("last_checkin_day") == day_key():
            return ActionResult(True, "checkin", "already checked in today")
        result = self.desktop.claim_daily(self.cfg["dom_fallback"]["checkin"])
        if result.ok:
            self.state.claim_once("checkin", "daily")
            self.state.kv_set("last_checkin_day", day_key())
        self.state.log("checkin", result.ok, result.detail)
        return result

    # ---- 2. 签到礼物自动兑换 ------------------------------------------------
    def claim_gifts(self) -> list[ActionResult]:
        if not self.cfg["gifts"]["enabled"]:
            return []
        if self.growth is None:
            return [ActionResult(
                False, "gift",
                "growth channel not configured (set [growth].enabled=true)")]
        results = [self.growth.claim_gift()]
        if self.cfg.get("growth", {}).get("auto_exchange", True):
            exchanges = self.growth.claim_exchanges(
                lambda key: self.state.already_claimed("exchange", key)
            )
            for ex in exchanges:
                if ex.ok and not ex.data.get("skipped") and ex.data.get("count", 1) != 0:
                    self.state.claim_once("exchange", ex.data.get("key", ex.detail))
                results.append(ex)
        for r in results:
            self.state.log(r.action, r.ok, r.detail[:400])
        return results

    # ---- 3. 派遣 / 到期领奖 / 再派遣 ----------------------------------------
    def travel_tick(self, force: bool = False) -> ActionResult:
        cfg = self.cfg["travel"]
        if not cfg["enabled"]:
            return ActionResult(True, "travel", "disabled")
        if self.growth is None:
            return ActionResult(
                False, "travel",
                "growth channel not configured (set [growth].enabled=true)")
        # 每次都从页面读真实倒计时；派遣后时长不固定，绝不套用固定间隔
        result = self.growth.tick(force=force)
        due = result.data.get("due_at")
        if due is not None:
            self.state.kv_set("travel_due_at", float(due))
        self.state.log("travel", result.ok, result.detail[:600])
        return result

    # ---- 全流程 -------------------------------------------------------------
    def reconcile_all(self, reason: str = "manual") -> dict[str, Any]:
        checkin = self.daily_checkin()
        gifts = self.claim_gifts()
        travel = self.travel_tick()
        summary = {
            "reason": reason,
            "checkin": checkin.as_dict(),
            "gifts": [r.as_dict() for r in gifts],
            "travel": travel.as_dict(),
            "ok": checkin.ok and travel.ok,
            "day": day_key(),
        }
        self.state.log("reconcile", summary["ok"], json.dumps(summary, ensure_ascii=False)[:800])
        logger.info("reconcile(%s) ok=%s", reason, summary["ok"])
        return summary
