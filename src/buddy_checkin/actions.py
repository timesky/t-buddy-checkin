"""业务编排：签到 → 漏签补签 → 兑换 → 抽奖 → 派遣/领奖。

每个任务都返回 ActionResult，并且 **data["state"]** 明确表达当天的可终止性：

    done          已用可复核证据确认今天做完了
    impossible    今天确实做不了（补登卡 0 / 没有抽奖券 / 派遣次数用尽）
    pending       还没成功或没有确定性结论，稍后重试

账本（progress.py）按这个 state 记录；只有全部 done/impossible 才静默到 23:59。
判断依据都落在「服务端状态」或「DOM 事实变化」上，绝不用「按钮不在=做完了」
这类推测（2026-11-13 就是这样把漏签记成已签的）。

通道分工（2026-11-14 实测，5.11.3）：
  - 每日签到    : 主窗口左下角「Buddy加油站」浮层（FuelCard），判据取 React
                  fiber 上的 checkinData（服务端 /checkin-activity-status）
  - 漏签补签    : 成长中心日历漏签格 + 补登卡（GrowthBrowser.makeup），
                  补签格只认真实鼠标事件（CDP Input.dispatchMouseEvent）
  - 礼物/派遣/兑换: GrowthBrowser 直连成长中心 iframe
  - 抽奖        : 成长中心「抽奖(N)」按钮，持续抽到券为 0

旧 CuaDriver / AX 通道已不再装配；不自动登录、不碰「去完成」类任务。
"""
from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import Any

from .adapters.base import ActionResult
from .progress import DONE, IMPOSSIBLE, PENDING
from .timeutil import day_key

logger = logging.getLogger(__name__)


def _state(result: ActionResult) -> str:
    return str((result.data or {}).get("state") or (DONE if result.ok else PENDING))


class Actions:
    def __init__(self, cfg: dict, state: Any, desktop: Any, growth: Any = None,
                 fuel: Any = None) -> None:
        self.cfg = cfg
        self.state = state
        self.desktop = desktop
        # growth 为 GrowthBrowser（CDP）：礼物/派遣/兑换/补签/抽奖
        self.growth = growth
        # fuel 为 FuelCard（CDP）：左下角「Buddy加油站」浮层，签到的唯一真实入口
        self.fuel = fuel

    # ---- 0. 端口：每轮开工前重新对齐客户端实际端口 --------------------------
    def refresh_port(self) -> int | None:
        """客户端可能中途重启换了端口；每轮验证/自检开头重探一次。

        只读 + 只改内存里的端口，不写文件、不重启客户端。
        """
        try:
            from . import ports

            res = ports.resolve(self.cfg, timeout=0.4)
        except Exception:  # noqa: BLE001 - 端口探测失败不该让整轮任务崩掉
            return None
        if not res.ok:
            return None
        growth = self.cfg.setdefault("growth", {})
        growth["cdp_port"] = res.port
        growth["cdp_port_source"] = res.source
        for adapter in (self.growth, self.fuel):
            if adapter is not None and getattr(adapter, "port", None) != res.port:
                logger.info("cdp: %s 端口 %s → %s（%s）",
                            type(adapter).__name__, getattr(adapter, "port", "?"),
                            res.port, res.source)
                adapter.port = res.port
                if hasattr(adapter, "_owner_ok"):
                    adapter._owner_ok = False
        return res.port


    # ---- 1. 每日签到（点左下角浮层的「立即领取」，服务端状态确认） ----------
    def daily_checkin(self) -> ActionResult:
        if not self.cfg["daily_checkin"]["enabled"]:
            return ActionResult(True, "checkin", "disabled",
                                {"confirmed": True, "state": DONE})
        if self.fuel is None:
            return ActionResult(False, "checkin",
                                "fuel card channel not configured (set [growth].enabled=true)",
                                {"confirmed": False, "state": PENDING})
        result = self.fuel.claim()
        if result.ok and result.data.get("confirmed"):
            self.state.claim_once("checkin", "daily")
            self.state.kv_set("last_checkin_day", day_key())
            self.state.kv_set("last_checkin_at", time.time())
            result.data["state"] = DONE
            if result.data.get("already"):
                result.detail = result.detail  # 服务端确认已签，同样算完成
        elif not result.ok:
            result.data.setdefault("state", PENDING)
        self.state.log("checkin", result.ok, result.detail)
        return result

    # ---- 2. 漏签补签（成长中心日历 + 补登卡） -------------------------------
    def missed_makeup(self) -> ActionResult:
        growth = self.cfg.get("growth", {}) or {}
        if not growth.get("enabled", True):
            return ActionResult(True, "makeup", "growth disabled",
                                {"state": IMPOSSIBLE})
        if not growth.get("auto_makeup", True):
            return ActionResult(True, "makeup", "auto_makeup disabled",
                                {"state": IMPOSSIBLE})
        if self.growth is None:
            return ActionResult(False, "makeup", "growth channel not configured",
                                {"state": PENDING})
        result = self.growth.makeup()
        result.data.setdefault("state", DONE if result.ok else PENDING)
        self.state.log("makeup", result.ok, result.detail[:400])
        return result

    # ---- 3. 兑换奖励 ---------------------------------------------------------
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
        if not self.cfg.get("growth", {}).get("auto_exchange", True):
            return ActionResult(True, "exchange", "auto_exchange disabled",
                                {"skipped": True, "count": 0, "state": DONE})
        if self.growth is None:
            return ActionResult(False, "exchange",
                                "growth channel not configured (set [growth].enabled=true)",
                                {"state": PENDING})
        results = self.growth.claim_exchanges(
            lambda key: self.state.already_claimed("exchange", key)
        )
        for ex in results:
            if ex.ok and not ex.data.get("skipped") and ex.data.get("count", 1) != 0:
                self.state.claim_once("exchange", ex.data.get("key", ex.detail))
            self.state.log(ex.action, ex.ok, ex.detail[:400])
        ok = all(r.ok for r in results) if results else True
        detail = "; ".join(r.detail[:100] for r in results) or "nothing exchangeable"
        return ActionResult(ok, "exchange", detail,
                            {"count": len(results), "state": DONE if ok else PENDING})

    # ---- 4. 抽奖 -------------------------------------------------------------
    def draw_lottery(self) -> ActionResult:
        growth = self.cfg.get("growth", {}) or {}
        if not growth.get("auto_lottery", True):
            return ActionResult(True, "lottery", "auto_lottery disabled",
                                {"state": IMPOSSIBLE})
        if self.growth is None:
            return ActionResult(False, "lottery", "growth channel not configured",
                                {"state": PENDING})
        result = self.growth.lottery()
        result.data.setdefault("state", DONE if result.ok else PENDING)
        self.state.log("lottery", result.ok, result.detail[:400])
        return result

    # ---- 5. 派遣 / 到期领奖 / 再派遣（滚动） --------------------------------
    def travel_tick(self, force: bool = False) -> ActionResult:
        cfg = self.cfg["travel"]
        if not cfg["enabled"]:
            return ActionResult(True, "travel", "disabled", {"state": IMPOSSIBLE})
        if self.growth is None:
            return ActionResult(
                False, "travel",
                "growth channel not configured (set [growth].enabled=true)",
                {"state": PENDING})
        # 每次都从页面读真实倒计时；派遣后时长不固定，绝不套用固定间隔
        result = self.growth.tick(force=force)
        due = result.data.get("due_at")
        if due is not None:
            self.state.kv_set("travel_due_at", float(due))
        # 注意：适配器 data 里的 "state" 是**页面状态**（idle/traveling/...），
        # 与账本的任务状态同名字不同义，这里必须显式覆盖成任务状态。
        result.data["page_state"] = result.data.pop("state", None)
        if result.data.get("dispatch_blocked") == "exhausted":
            # 今天次数用尽且没有可领奖励：派遣任务今天结束了
            result.data["state"] = DONE
        else:
            result.data["state"] = PENDING
        self.state.log("travel", result.ok, result.detail[:600])
        return result

    # ---- 批量会话：多个任务共用一次打开的成长中心页面 ----------------------
    def growth_session(self):
        """补签/兑换/抽奖/派遣都在成长中心里，合并成一次打开，少开关窗口。"""
        sess = getattr(self.growth, "session", None)
        if callable(sess):
            return sess()
        return contextlib.nullcontext()

    # ---- 全流程（重启自检 / 手动 reconcile） --------------------------------
    def reconcile_all(self, reason: str = "manual") -> dict[str, Any]:
        """按顺序跑完五个任务，返回汇总。

        顺序有依赖：签到 → 补签（会解锁连登里程碑）→ 兑换（可能发抽奖券）
        → 抽奖 → 派遣（独立滚动）。
        """
        out: dict[str, Any] = {"reason": reason, "day": day_key()}
        tasks = (
            ("checkin", self.daily_checkin),
            ("makeup", self.missed_makeup),
            ("exchange", self.check_exchanges),
            ("lottery", self.draw_lottery),
            ("travel", self.travel_tick),
        )
        with self.growth_session():
            for name, fn in tasks:
                try:
                    result = fn()
                    out[name] = result.as_dict()
                    out[name]["state"] = _state(result)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("task %s crashed: %s", name, exc)
                    out[name] = {"ok": False, "detail": f"crashed: {exc}",
                                 "state": PENDING}
        out["ok"] = all(v.get("state") in (DONE, IMPOSSIBLE) for v in out.values()
                        if isinstance(v, dict) and "state" in v)
        self.state.log("reconcile", out["ok"],
                       json.dumps({k: v.get("state") for k, v in out.items()
                                   if isinstance(v, dict) and "state" in v},
                                  ensure_ascii=False))
        return out
