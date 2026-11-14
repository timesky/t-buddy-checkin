"""业务编排：签到 -> 礼物兑换 -> 派遣 -> 精确到期领礼物 -> 再次派遣。

所有领取都经 State.claim_once 幂等。**全部走 CDP（agent-browser + 客户端调试端口）**：

  - 每日签到      : 点主窗口左下角「Buddy加油站」浮层的「立即领取」（FuelCard）。
                    2026-11-14 更正：签到从来没在成长中心，之前改成「读活跃地图」
                    是误判 —— 活跃地图只记录有没有用客户端，和签到领积分无关，
                    结果整条签到线一次都没执行却报 confirmed=True。
  - 礼物/派遣/兑换  : GrowthBrowser 直连成长中心 iframe，DOM 级 click

旧的 CuaDriver / AX 通道（desktop.CuaDriverSigner、growth.GrowthCenter）
已不再被 build() 装配，配置文件里的 ax 相关键仅作历史兼容。
不自动登录、不消耗补签卡、不做「去完成」类任务。
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
    def __init__(self, cfg: dict, state: Any, desktop: Any, growth: Any = None,
                 fuel: Any = None) -> None:
        self.cfg = cfg
        self.state = state
        self.desktop = desktop
        # growth 为 GrowthBrowser（CDP）：礼物/派遣/兑换
        self.growth = growth
        # fuel 为 FuelCard（CDP）：左下角「Buddy加油站」浮层，签到的唯一真实入口
        self.fuel = fuel

    # ---- 1. 每日签到（点左下角浮层的「立即领取」） --------------------------
    def daily_checkin(self) -> ActionResult:
        """签到时间线的一轮：点左下角 Buddy加油站 的「立即领取」。

        2026-11-14 更正（5.11.3 实测）：
          - 签到入口只在主窗口左下角浮层（div.daily-checkin 的 shadow DOM 内），
            点 #go 经 bridge 事件走 claimDailyCheckin 接口；
          - 成长中心「11月活跃地图」只是活跃记录，**不能**当签到判据 —— 之前
            按它判 confirmed=True，导致签到整条线从没真正执行过；
          - 漏签只能在成长中心日历用补登卡补，不自动消耗（本方法不碰）。

          - 领取成功   -> ok=True, confirmed=True，落库 last_checkin_day
          - 今日已领   -> ok=True, confirmed=True（already），不重复点
          - 浮层不在   -> ok=False，不落库，下一轮再试
        """
        if not self.cfg["daily_checkin"]["enabled"]:
            return ActionResult(True, "checkin", "disabled", {"confirmed": True})
        if self.state.kv_get("last_checkin_day") == day_key():
            return ActionResult(True, "checkin", "already checked in today",
                                {"confirmed": True})
        if self.fuel is None:
            return ActionResult(False, "checkin",
                                "fuel card channel not configured (set [growth].enabled=true)",
                                {"confirmed": False})
        result = self.fuel.claim()
        if result.ok and result.data.get("confirmed"):
            self.state.claim_once("checkin", "daily")
            self.state.kv_set("last_checkin_day", day_key())
            self.state.kv_set("last_checkin_at", time.time())
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

    def check_exchanges(self) -> ActionResult:
        """兑换检查时间线的一轮：只检查/兑换，不碰签到与派遣。

        ok=True 表示得到确定结论（兑换成功，或确认无可兑换），调度器据此
        禁用到当天 23:59；检查失败/窗口不可用则 ok=False，隔一小时重试。
        """
        if not self.cfg.get("growth", {}).get("auto_exchange", True):
            return ActionResult(True, "exchange", "auto_exchange disabled",
                                {"skipped": True, "count": 0})
        if self.growth is None:
            return ActionResult(False, "exchange",
                                "growth channel not configured (set [growth].enabled=true)")
        results = self.growth.claim_exchanges(
            lambda key: self.state.already_claimed("exchange", key)
        )
        for ex in results:
            if ex.ok and not ex.data.get("skipped") and ex.data.get("count", 1) != 0:
                self.state.claim_once("exchange", ex.data.get("key", ex.detail))
            self.state.log(ex.action, ex.ok, ex.detail[:400])
        ok = all(r.ok for r in results) if results else True
        detail = "; ".join(r.detail[:100] for r in results) or "nothing exchangeable"
        return ActionResult(ok, "exchange", detail, {"count": len(results)})

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
