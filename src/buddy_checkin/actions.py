"""业务编排：签到 -> 礼物兑换 -> 派遣 -> 精确到期领礼物 -> 再次派遣。

所有领取都经 State.claim_once 幂等；桌面端只被 daily_checkin 使用。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from .adapters.base import ActionResult
from .timeutil import day_key, parse_countdown, travel_due

logger = logging.getLogger(__name__)

# 真实登录页特征（实测 AX 快照含 Iframe "wechat-qrcode" 与「微信扫码登录」）
LOGIN_MARKERS = ("微信扫码登录", "wechat-qrcode", "其他登录方式")

_COUNTDOWN = re.compile(r"\b(\d{1,2}):(\d{2}):(\d{2})\b")

# 可选：配置了状态接口时，从页面上下文带 cookie 直接取结构化 JSON
_STATUS_JS = """(async () => {
  try {
    const r = await fetch(%s, {credentials: 'include'});
    const j = await r.json();
    return JSON.stringify(j).slice(0, 8000);
  } catch (e) { return 'FETCH_FAILED'; }
})()"""


class Actions:
    def __init__(self, cfg: dict, state: Any, web: Any, desktop: Any, growth: Any = None) -> None:
        self.cfg = cfg
        self.state = state
        self.web = web
        self.desktop = desktop
        # growth 不为空时，礼物/派遣改走 CuaDriver 的成长中心 AX 通道
        self.growth = growth
        self.dom = cfg["dom_fallback"]

    # ---- 1. 每日签到（唯一依赖桌面客户端） ---------------------------------
    def daily_checkin(self) -> ActionResult:
        if not self.cfg["daily_checkin"]["enabled"]:
            return ActionResult(True, "checkin", "disabled")
        if self.state.kv_get("last_checkin_day") == day_key():
            return ActionResult(True, "checkin", "already checked in today")
        result = self.desktop.claim_daily(self.dom["checkin"])
        if result.ok:
            self.state.claim_once("checkin", "daily")
            self.state.kv_set("last_checkin_day", day_key())
        self.state.log("checkin", result.ok, result.detail)
        return result

    # ---- 2. 签到礼物自动兑换 ------------------------------------------------
    def claim_gifts(self) -> list[ActionResult]:
        if not self.cfg["gifts"]["enabled"]:
            return []
        if self.growth is not None:
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
        snapshot = self.web.snapshot()
        if any(marker in snapshot for marker in LOGIN_MARKERS):
            return [
                ActionResult(False, "gift",
                             "网页未登录：请在浏览器登录 WorkBuddy 后重试（本技能不自动登录）")
            ]
        allow = self.cfg["gifts"]["allow_keywords"]
        deny = self.cfg["gifts"]["deny_keywords"]
        results: list[ActionResult] = []
        for text in self.dom["gift_claim"]:
            if len(results) >= int(self.cfg["gifts"]["max_claims_per_run"]):
                break
            if any(word in text for word in deny) or not any(word in text for word in allow):
                continue
            if self.state.already_claimed("gift", text):
                continue
            clicked = self.web.click_text([text])
            if clicked:
                self.state.claim_once("gift", text)
                results.append(ActionResult(True, "gift", f"claimed: {text}"))
                self.state.log("gift", True, text)
            else:
                results.append(ActionResult(False, "gift", f"no claimable button: {text}"))
        return results

    # ---- 3. 派遣状态 --------------------------------------------------------
    def read_travel_status(self) -> dict[str, Any]:
        status: dict[str, Any] = {"state": "unknown", "due_at": None, "source": "dom"}
        endpoint = self.cfg["web"].get("status_endpoint", "")
        if endpoint:
            raw = self.web.eval_js(_STATUS_JS % json.dumps(endpoint))
            if raw and "FETCH_FAILED" not in raw:
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = None
                if payload is not None:
                    status["source"] = "api"
                    status["raw"] = str(payload)[:1000]
                    status["due_at"] = travel_due(payload, self.cfg["travel"], time.time())
                    state_val = str(
                        self._find_state(payload, self.cfg["travel"]["state_field_candidates"])
                        or ""
                    ).lower()
                    for name, values in self.cfg["travel"]["state_values"].items():
                        if state_val in [v.lower() for v in values]:
                            status["state"] = name
                            break
        snapshot = self.web.snapshot()
        status["snapshot"] = snapshot[:2000]
        if any(marker in snapshot for marker in LOGIN_MARKERS):
            status["state"] = "login_required"
            return status
        if status["state"] == "unknown":
            if any(word in snapshot for word in self.dom["claim_travel"]):
                status["state"] = "claimable"
            elif any(word in snapshot for word in self.dom["traveling"]):
                status["state"] = "traveling"
            elif any(word in snapshot for word in self.dom["dispatch"]):
                status["state"] = "idle"
        if status["due_at"] is None:
            match = _COUNTDOWN.search(snapshot)
            if match:
                remain = parse_countdown(
                    f"{int(match.group(1)):02d}:{match.group(2)}:{match.group(3)}"
                )
                if remain is not None:
                    status["due_at"] = time.time() + remain
        return status

    @staticmethod
    def _find_state(payload: Any, names: list[str]) -> Any:
        from .timeutil import find_field

        return find_field(payload, names)

    # ---- 4. 派遣 / 到期领奖 / 再派遣 ----------------------------------------
    def travel_tick(self, force: bool = False) -> ActionResult:
        cfg = self.cfg["travel"]
        if not cfg["enabled"]:
            return ActionResult(True, "travel", "disabled")
        if self.growth is not None:
            # 每次都从页面读真实倒计时；派遣后时长不固定，绝不套用固定间隔
            result = self.growth.tick(force=force)
            due = result.data.get("due_at")
            if due is not None:
                self.state.kv_set("travel_due_at", float(due))
            self.state.log("travel", result.ok, result.detail[:600])
            return result
        status = self.read_travel_status()
        state = "claimable" if force else status["state"]
        result = ActionResult(True, "travel", f"state={state}", {"status": status})

        if state == "login_required":
            return ActionResult(
                False,
                "travel",
                "网页未登录：请在浏览器登录 WorkBuddy 后重试（本技能不自动登录）",
                {"status": status},
            )

        if state == "claimable":
            claimed = self.web.click_text(self.dom["claim_travel"])
            due_key = str(int(status.get("due_at") or time.time()))
            if claimed:
                self.state.claim_once("travel_gift", due_key)
                result.detail = "travel gift claimed"
            else:
                result.ok = False
                result.detail = "claim button not found"
            if cfg["re_dispatch"]:
                dispatched = self.web.click_text(self.dom["dispatch"])
                result.data["re_dispatched"] = dispatched
                self._record_next_due(dispatched)
            self.state.log("travel_claim", result.ok, result.detail)
            return result

        if state == "traveling":
            due = status.get("due_at")
            if due:
                self.state.kv_set("travel_due_at", float(due) + float(cfg["claim_offset_sec"]))
                result.detail = f"traveling, due recorded"
            else:
                self.state.kv_set(
                    "travel_due_at", time.time() + float(cfg["fallback_interval_sec"])
                )
                result.detail = "traveling, due unknown, fallback scheduled"
            return result

        if state == "idle":
            dispatched = self.web.click_text(self.dom["dispatch"])
            result.ok = dispatched
            result.detail = "dispatched" if dispatched else "dispatch button not found"
            self._record_next_due(dispatched)
            self.state.log("travel_dispatch", result.ok, result.detail)
            return result

        result.ok = False
        result.detail = "travel state unknown; no action taken"
        return result

    def _record_next_due(self, dispatched: bool) -> None:
        if not dispatched:
            self.state.kv_set(
                "travel_due_at", time.time() + float(self.cfg["travel"]["fallback_interval_sec"])
            )
            return
        follow_up = self.read_travel_status()
        due = follow_up.get("due_at")
        if due is None:
            due = time.time() + float(self.cfg["travel"]["fallback_interval_sec"])
        self.state.kv_set("travel_due_at", float(due) + float(self.cfg["travel"]["claim_offset_sec"]))

    # ---- 全流程 -------------------------------------------------------------
    def ensure_web_ready(self) -> str:
        """真实 session 重开后可能是空白页；空快照时显式 open 并等待渲染。

        实测 bug：once/daemon 原先从不 open，travel 只读到 (no interactive elements)。
        """
        snap = self.web.snapshot()
        if not snap.strip() or "no interactive elements" in snap:
            self.web.open()
            self.web.wait(3000)
            snap = self.web.snapshot()
        return snap

    def reconcile_all(self, reason: str = "manual") -> dict[str, Any]:
        if self.growth is None:
            self.ensure_web_ready()
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
