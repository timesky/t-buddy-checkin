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
    def _find(payload: dict, texts: list[str]) -> dict | None:
        """返回匹配的元素（含 element_token / element_index），而不只是索引。"""
        for el in payload.get("elements", []):
            label = str(el.get("label") or "")
            if el.get("role") in ("AXButton", "AXMenuItem") and any(t in label for t in texts):
                return el
        return None

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
        """签到：先确保加油站面板展开，再用纯 AX 路径点「立即领取」并复核。

        实测 2026-11-10：面板收起时 AX 树里只有入口按钮「Buddy加油站」，
        **没有**「立即领取」。旧逻辑把「按钮找不到」当成已签到 → 假阳性，
        整天没签却写了 last_checkin_day。现在必须先展开面板再判断。
        """
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
        if btn is None:
            entry = self._find(snap, self.STATION_ENTRY)
            if entry is not None:
                failure = self._click_element(entry, pid, wid)
                if failure is not None:
                    return ActionResult(False, "desktop.checkin",
                                        f"open station panel: {failure.detail}")
                expanded = True
                # element_index 缓存会被新快照替换，必须重新取快照再定位
                snap = self._snapshot(pid, wid)
                if "error" in snap:
                    return ActionResult(False, "desktop.checkin", snap["error"],
                                        {"backend": "cua-driver"})
                btn = self._find(snap, texts)

        if btn is None:
            if expanded:
                # 面板已展开仍无领取按钮 = 今天确实已领
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
                                {"backend": "cua-driver", "confirmed": False})
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
