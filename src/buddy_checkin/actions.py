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
        """签到时间线的一轮：只做签到，不碰成长中心。

        返回 data.confirmed：True 才允许当天禁用轮询。
        只有「本轮点击并验证按钮消失」或「面板展开后确认无领取按钮」才算确认；
        仅凭按钮不存在推测、或对话正文里的『明日再来』都不得落库。

        满 24 小时口径（2026-11-12 用户要求）：即使跨了北京时间 0 点，若距
        上次真实签到不足 min_interval_sec（默认 24h），仍不认定已签，继续轮询，
        避免凌晨因活动尚未刷新而把旧状态当作新一天的签到。
        """
        if not self.cfg["daily_checkin"]["enabled"]:
            return ActionResult(True, "checkin", "disabled", {"confirmed": True})
        cfg = self.cfg.get("daily_checkin", {})
        min_interval = float(cfg.get("min_interval_sec", 24 * 3600))
        last_at = self.state.kv_get_float("last_checkin_at")
        now = time.time()
        if last_at is not None and 0 <= now - last_at < min_interval:
            left = int(min_interval - (now - last_at))
            return ActionResult(
                True, "checkin",
                f"last real checkin < {int(min_interval/3600)}h ago; wait {left}s",
                {"confirmed": True, "waiting_cooldown": True, "last_checkin_at": last_at},
            )
        if self.state.kv_get("last_checkin_day") == day_key():
            return ActionResult(True, "checkin", "already checked in today",
                                {"confirmed": True})
        result = self.desktop.claim_daily(self.cfg["dom_fallback"]["checkin"])
        # 只有「确认」才落库：面板收起导致按钮找不到、或对话正文干扰，
        # 都绝不写 last_checkin_day / last_checkin_at，否则整天不再补签。
        if result.ok and result.data.get("confirmed"):
            self.state.claim_once("checkin", "daily")
            self.state.kv_set("last_checkin_day", day_key())
            self.state.kv_set("last_checkin_at", now)
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
