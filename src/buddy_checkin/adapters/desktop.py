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
    def _find_button(payload: dict, texts: list[str]) -> int | None:
        for el in payload.get("elements", []):
            label = str(el.get("label") or "")
            if el.get("role") in ("AXButton", "AXMenuItem") and any(t in label for t in texts):
                return el.get("element_index")
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
        idx = self._find_button(snap, ["立即领取", "今日礼包"])
        return ActionResult(
            True, "desktop.probe",
            "checkin button visible" if idx is not None
            else "checkin button absent (likely already checked in today)",
            {"backend": "cua-driver", "pid": pid, "window_id": wid,
             "element_count": snap.get("element_count"), "checkin_button_index": idx},
        )

    def claim_daily(self, texts: list[str]) -> ActionResult:
        win = self._window()
        if not win:
            return ActionResult(False, "desktop.checkin", "WorkBuddy main window not found",
                               {"backend": "cua-driver"})
        pid, wid = win
        snap = self._snapshot(pid, wid)
        if "error" in snap:
            return ActionResult(False, "desktop.checkin", snap["error"],
                               {"backend": "cua-driver"})
        idx = self._find_button(snap, texts)
        if idx is None:
            # 签到成功后 Buddy 加油站卡片会收起，按钮不再出现
            return ActionResult(
                True, "desktop.checkin",
                "checkin button absent; treating as already checked in today",
                {"backend": "cua-driver", "clicked": False, "already": True},
            )
        result = self._call("click", {"pid": pid, "window_id": wid, "element_index": idx})
        if "error" in result:
            return ActionResult(False, "desktop.checkin", result["error"],
                               {"backend": "cua-driver", "element_index": idx})
        after = self._snapshot(pid, wid)
        still = self._find_button(after, texts)
        ok = still is None
        return ActionResult(
            ok, "desktop.checkin",
            "clicked and button disappeared (checkin verified)" if ok
            else "clicked but button still visible",
            {"backend": "cua-driver", "clicked": True, "element_index": idx,
             "verified": ok, "element_count_before": snap.get("element_count"),
             "element_count_after": after.get("element_count")},
        )
