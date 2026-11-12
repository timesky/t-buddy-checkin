"""桌面端签到适配器：唯一依赖 WorkBuddy 客户端的环节。

只用 **CuaDriver**（trycua 出品，com.trycua.driver AX 通道）——本机唯一拿到 TCC
辅助功能授权（auth=2）的自动化后端。其它候选（agent-desktop / cdp / computer-use）
均未授权、未使用，已从本技能移除。

实测 2026-11-09：WorkBuddy 客户端 AX 树的 Buddy 加油站卡片里含
AXButton "立即领取"；用 element_index 点击走 AX press，
不移动光标、不抢焦点，不干扰人工操作。点击后卡片与按钮消失即签到成功。
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .base import ActionResult, DesktopAdapter

CUA_DRIVER_BIN = "/Applications/CuaDriver.app/Contents/MacOS/cua-driver"


class CuaDriverSigner(DesktopAdapter):
    """通过已授权的 CuaDriver (com.trycua.driver) 用 AX 路径点击签到按钮。"""

    def __init__(self, cfg: dict, runner: Callable[..., Any] | None = None) -> None:
        desk = cfg["desktop"]
        self.bin = desk.get("cua_driver_bin") or CUA_DRIVER_BIN
        self.app = desk.get("app", "WorkBuddy")
        self.station_entry_labels = desk.get("station_entry_labels") or ["Buddy加油站"]
        self.already_claimed_labels = desk.get("already_claimed_labels") or [
            "今日已领取", "已领取", "明日再来", "今日已领",
        ]
        # 面板区索引窗口：入口 idx + 该值 = 面板上界，超出即视为对话正文，不参与已签判定
        self.panel_window = int(desk.get("station_panel_window", 16))
        # AXPress -25202 = kAXErrorInvalidUIElement（元素引用失效）。
        # 常见于另一台机器/客户端刚重绘：重新取快照定位再点即可恢复。
        self.max_attempts = int(desk.get("click_max_attempts", 3))
        self.retry_delay = float(desk.get("click_retry_delay_sec", 0.8))
        self.runner = runner or subprocess.run
        self.commands: list[list[str]] = []

    def _call(self, tool: str, payload: dict) -> dict:
        cmd = [self.bin, "call", tool, json.dumps(payload, ensure_ascii=False)]
        self.commands.append(cmd)
        try:
            proc = self.runner(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"error": str(exc)}
        raw = getattr(proc, "stdout", "") or ""
        i = raw.find("{")
        if i < 0:
            return {"error": (raw or getattr(proc, "stderr", "") or "empty output")[:200]}
        try:
            return json.loads(raw[i:])
        except ValueError:
            return {"error": "invalid JSON from cua-driver"}

    def _window(self) -> tuple[int, int] | None:
        """找 WorkBuddy 可见主窗口，返回 (pid, window_id)。"""
        data = self._call("list_windows", {})
        wins = [
            w for w in data.get("windows", [])
            if w.get("app_name") == self.app
            and w.get("is_on_screen")
            and (w.get("bounds") or {}).get("width", 0) > 400
        ]
        if not wins:
            return None
        w = max(wins, key=lambda x: x["bounds"]["width"] * x["bounds"]["height"])
        return int(w["pid"]), int(w["window_id"])

    def _snapshot(self, pid: int, wid: int) -> dict:
        # 每轮 element_index 动作前必须重新索引，否则索引映射失效
        return self._call("get_window_state", {
            "pid": pid, "window_id": wid,
            "include_screenshot": False, "max_elements": 2000,
        })

    @staticmethod
    def _find(payload: dict, texts: list[str],
              roles: tuple[str, ...] | None = ("AXButton", "AXMenuItem"),
              max_index: int | None = None,
              exact: bool = False) -> dict | None:
        """返回匹配的元素（含 element_token / element_index），而不只是索引。

        roles 只认按钮类（默认）；max_index 限定搜索的元素索引上界，用于把
        已签证据锁在「加油站面板区」，排除右侧对话正文（会含「明日再来」等
        历史消息，2026-11-12 假阳性根因）。exact=True 时整标签精确匹配。
        """
        for el in payload.get("elements", []):
            label = str(el.get("label") or "")
            lab = label.strip()
            if roles is not None and el.get("role") not in roles:
                continue
            if max_index is not None and (el.get("element_index") or 0) > max_index:
                continue
            matched = (any(t == lab for t in texts) if exact
                       else any(t in label for t in texts))
            if matched:
                return el
        return None

    def _panel_upper_bound(self, snap: dict, entry_index: int | None) -> int | None:
        """加油站面板元素的索引上界：入口之后的「消息中心/对话内搜索」等为界。

        实测 5.11.2：面板区 idx 50-63，对话正文从 idx 71 起。用入口索引 +
        一个保守窗口（默认 64）界定，避免把正文纳入已签判定。
        """
        if entry_index is None:
            return None
        return entry_index + self.panel_window

    # 签到按钮藏在「Buddy加油站」面板里，面板收起时按钮根本不在 AX 树中。
    # 必须先点开这个入口，再重新快照找「立即领取」。
    STATION_ENTRY = ["Buddy加油站"]

    def _click_element(self, el: dict, pid: int, wid: int) -> ActionResult | None:
        """纯 AX 路径点击；返回 None 表示成功，否则返回失败结果。

        element_token 来自最近一次快照，失效时 CuaDriver 会显式报 stale，
        不会静默回退到坐标点击（那会把光标甩到窗口左上角）。
        同时校验返回的 path == 'ax'，任何像素路径一律视为失败并拒绝。
        """
        payload: dict[str, Any] = {"pid": pid, "window_id": wid}
        if el.get("element_token"):
            payload["element_token"] = el["element_token"]
        elif el.get("element_index") is not None:
            payload["element_index"] = el["element_index"]
        else:
            return ActionResult(False, "desktop.click", "element has no token/index")
        out = self._call("click", payload)
        if "error" in out:
            return ActionResult(False, "desktop.click", str(out["error"]), {"payload": payload})
        if out.get("path") not in (None, "ax"):
            # 非 AX 路径会合成鼠标事件、移动光标，坚决拒绝
            return ActionResult(False, "desktop.click",
                                f"refused non-AX click path: {out.get('path')}",
                                {"payload": payload})
        return None

    def probe(self) -> ActionResult:
        if not Path(self.bin).exists():
            return ActionResult(False, "desktop.probe", "cua-driver binary not found",
                               {"backend": "cua-driver"})
        win = self._window()
        if not win:
            return ActionResult(False, "desktop.probe", "WorkBuddy main window not found",
                               {"backend": "cua-driver"})
        pid, wid = win
        snap = self._snapshot(pid, wid)
        if "error" in snap:
            return ActionResult(False, "desktop.probe", snap["error"],
                               {"backend": "cua-driver", "pid": pid, "window_id": wid})
        btn = self._find(snap, ["立即领取", "今日礼包"])
        return ActionResult(
            True, "desktop.probe",
            "checkin button visible" if btn is not None
            else "checkin button absent (station panel may be collapsed)",
            {"backend": "cua-driver", "pid": pid, "window_id": wid,
             "element_count": snap.get("element_count"),
             "checkin_button_index": btn.get("element_index") if btn else None},
        )

    def claim_daily(self, texts: list[str]) -> ActionResult:
        """签到（带重试）：整轮失败就重新走一遍「取快照 → 展开 → 定位 → 点击」。

        另一台机器实测 2026-11-10 报 `AXUIElementPerformAction(AXPress)
        returned -25202`（kAXErrorInvalidUIElement，元素引用失效）。这类错误
        只能靠**重新快照重新定位**恢复，重试同一 token 必然再失败。
        """
        last: ActionResult | None = None
        for attempt in range(self.max_attempts):
            result = self._claim_once(texts)
            if result.ok:
                return result
            last = result
            if attempt + 1 < self.max_attempts:
                time.sleep(self.retry_delay)
        return last or ActionResult(False, "desktop.checkin", "no attempt made")

    def _claim_once(self, texts: list[str]) -> ActionResult:
        win = self._window()
        if not win:
            return ActionResult(False, "desktop.checkin", "WorkBuddy main window not found",
                                {"backend": "cua-driver"})
        pid, wid = win
        snap = self._snapshot(pid, wid)
        if "error" in snap:
            return ActionResult(False, "desktop.checkin", snap["error"],
                                {"backend": "cua-driver"})

        btn = self._find(snap, texts)
        expanded = False
        panel_max: int | None = None
        entry = self._find(snap, self.station_entry_labels)
        if entry is not None:
            panel_max = self._panel_upper_bound(snap, entry.get("element_index"))
        if btn is None:
            if entry is not None:
                failure = self._click_element(entry, pid, wid)
                if failure is not None:
                    return ActionResult(False, "desktop.checkin",
                                        f"open station panel: {failure.detail}",
                                        {"backend": "cua-driver", "retryable": True})
                expanded = True
                # element_index 缓存会被新快照替换，必须重新取快照再定位
                snap = self._snapshot(pid, wid)
                if "error" in snap:
                    return ActionResult(False, "desktop.checkin", snap["error"],
                                        {"backend": "cua-driver", "retryable": True})
                # 面板展开后入口按钮可能位移，重新定位锚点
                entry2 = self._find(snap, self.station_entry_labels)
                if entry2 is not None:
                    panel_max = self._panel_upper_bound(snap, entry2.get("element_index"))
                btn = self._find(snap, texts, max_index=panel_max)

        if btn is None:
            # 「今日已领取 / 明日再来」等文案是页面给出的强证据。
            # **只认加油站面板区内、按钮类 role**——绝不能全文搜索，否则会把
            # 客户端右侧对话正文（含「明日再来」等历史消息）误判为已签到。
            evidence = self._find(snap, self.already_claimed_labels,
                                  roles=("AXButton", "AXPopUpButton"),
                                  max_index=panel_max)
            if evidence is not None:
                return ActionResult(
                    True, "desktop.checkin",
                    f"already checked in today (panel shows {evidence.get('label')})",
                    {"backend": "cua-driver", "clicked": False, "already": True,
                     "confirmed": True, "evidence": evidence.get("label")},
                )
            if expanded:
                # 面板已展开、且面板区内没有领取按钮 = 今天确实已领
                return ActionResult(
                    True, "desktop.checkin",
                    "station panel open, no checkin button; already checked in today",
                    {"backend": "cua-driver", "clicked": False, "already": True,
                     "confirmed": True, "station_expanded": True},
                )
            # 连入口都找不到：不能推断，交给调度器隔一小时再查
            return ActionResult(
                False, "desktop.checkin",
                "checkin button and station entry both missing; state uncertain",
                {"backend": "cua-driver", "clicked": False, "confirmed": False,
                 "uncertain": True},
            )

        failure = self._click_element(btn, pid, wid)
        if failure is not None:
            return ActionResult(False, "desktop.checkin", failure.detail,
                                {"backend": "cua-driver", "confirmed": False,
                                 "retryable": True})
        after = self._snapshot(pid, wid)
        still = self._find(after, texts)
        ok = still is None
        return ActionResult(
            ok, "desktop.checkin",
            "clicked and button disappeared (checkin verified)" if ok
            else "clicked but button still visible",
            {"backend": "cua-driver", "clicked": True,
             "verified": ok, "confirmed": ok, "station_expanded": expanded,
             "element_count_before": snap.get("element_count"),
             "element_count_after": after.get("element_count")},
        )
