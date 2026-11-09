"""桌面端签到适配器：唯一依赖 WorkBuddy 客户端的环节。

按 agent-desktop 1.2.0 真实 CLI 校准（不是臆测的命令格式）：
  - 每个命令输出一个 JSON envelope：{"version","ok","command","data"|"error"}
  - snapshot: `snapshot --app <app> -i --compact --force-electron-a11y --session <s>`
  - ref 是全限定 `@<snapshot_id>:eN`，UI 变化后即失效，必须重新 snapshot
  - click: `click <qualified_ref> --session <s>`，click **没有** --app 参数
  - 默认 strict headless semantic delivery，不激活窗口、不移动光标

后端优先级：agent-desktop -> CDP(eval 穿透 Shadow DOM) -> computer-use(默认禁用)。
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any, Callable

from .base import ActionResult, DesktopAdapter

SHADOW_CLICK_JS = """(() => {
  const words = %s;
  function walk(root, depth) {
    if (!root || depth > 12) return null;
    for (const el of root.querySelectorAll('*')) {
      const t = (el.innerText || el.textContent || '').trim();
      if (t && t.length < 40 && words.some(w => t.includes(w))) {
        const clickable = el.tagName === 'BUTTON'
          || el.getAttribute('role') === 'button'
          || typeof el.onclick === 'function';
        if (clickable) { el.click(); return t; }
      }
      if (el.shadowRoot) {
        const hit = walk(el.shadowRoot, depth + 1);
        if (hit) return hit;
      }
    }
    return null;
  }
  return walk(document, 0) || 'NOT_FOUND';
})()"""

_LABEL_KEYS = ("name", "label", "title", "value", "description", "text", "role_description")


def find_ref(payload: Any, keywords: list[str]) -> str | None:
    """在结构化的 AX 树 JSON 里按可见名称找全限定 ref。"""
    stack: list[Any] = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            label = " ".join(str(cur.get(k, "") or "") for k in _LABEL_KEYS)
            ref = cur.get("ref")
            if isinstance(ref, str) and ref.startswith("@") and any(w in label for w in keywords):
                return ref
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(v for v in cur if isinstance(v, (dict, list)))
    return None


class DesktopSigner(DesktopAdapter):
    def __init__(self, cfg: dict, runner: Callable[..., Any] | None = None) -> None:
        desk = cfg["desktop"]
        self.engine = desk["engine"]
        self.bin = desk["bin"]
        self.app = desk["app"]
        self.cdp_port = int(desk["cdp_port"])
        self.allow_focus = bool(desk["allow_focus"])
        self.session = "buddy-checkin"
        self.runner = runner or subprocess.run
        self.commands: list[list[str]] = []

    # ---- 执行与 JSON envelope ----------------------------------------------
    def _exec(self, cmd: list[str]) -> tuple[bool, str]:
        self.commands.append(cmd)
        try:
            proc = self.runner(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"command failed: {exc}"
        out = (getattr(proc, "stdout", "") or "").strip()
        err = (getattr(proc, "stderr", "") or "").strip()
        ok = getattr(proc, "returncode", 1) == 0
        return ok, out if ok else (out + "\n" + err)[:800]

    def _exec_json(self, cmd: list[str]) -> tuple[bool, str, Any]:
        rc_ok, out = self._exec(cmd)
        try:
            envelope = json.loads(out)
        except (TypeError, ValueError):
            return rc_ok, out[:800], None
        if not isinstance(envelope, dict):
            return rc_ok, out[:800], None
        ok = bool(envelope.get("ok", rc_ok))
        error = envelope.get("error") or {}
        message = str(error.get("message", "")) if isinstance(error, dict) else str(error)
        code = str(error.get("code", "")) if isinstance(error, dict) else ""
        return ok, (f"{code} {message}".strip() or "ok"), envelope.get("data")

    # ---- 后端可用性 ---------------------------------------------------------
    def _cdp_open(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.cdp_port), timeout=0.3):
                return True
        except OSError:
            return False

    def backends(self) -> dict[str, bool]:
        return {
            "agent-desktop": shutil.which(self.bin) is not None,
            "cdp": self._cdp_open(),
            "computer-use": self.allow_focus,
        }

    def _choose(self) -> str | None:
        available = self.backends()
        if self.engine != "auto":
            return self.engine if available.get(self.engine) else None
        for name in ("agent-desktop", "cdp", "computer-use"):
            if available.get(name):
                return name
        return None

    def permissions(self) -> dict[str, Any]:
        """只读权限报告，不弹系统对话框。"""
        ok, _msg, data = self._exec_json([self.bin, "permissions"])
        return data if ok and isinstance(data, dict) else {}

    def _ax_snapshot(self) -> tuple[bool, str, Any]:
        # -i 只看可交互节点；--force-electron-a11y 针对 Chromium/Electron 渲染器
        return self._exec_json(
            [
                self.bin, "snapshot", "--app", self.app,
                "-i", "--compact", "--force-electron-a11y",
                "--session", self.session, "--timeout-ms", "10000",
            ]
        )

    # ---- 只读探测 -----------------------------------------------------------
    def probe(self) -> ActionResult:
        available = self.backends()
        chosen = self._choose()
        data: dict[str, Any] = {
            "chosen": chosen,
            "backends": available,
            "allow_focus": self.allow_focus,
        }
        if chosen == "agent-desktop":
            perms = self.permissions()
            data["permissions"] = {
                k: (v.get("state") if isinstance(v, dict) else v) for k, v in perms.items()
            }
            accessibility = data["permissions"].get("accessibility")
            if accessibility == "denied":
                return ActionResult(
                    False,
                    "desktop.probe",
                    "Accessibility permission denied; run '<bin> permissions --guide' and grant it to Terminal/WorkBuddy",
                    data,
                )
            ok, message, payload = self._ax_snapshot()
            data["snapshot"] = str(payload)[:4000]
            if not ok:
                return ActionResult(False, "desktop.probe", message, data)
            data["checkin_ref"] = find_ref(payload, ["立即领取", "今日礼包", "签到"])
            return ActionResult(
                True, "desktop.probe",
                "ax-tree ok; checkin ref found" if data["checkin_ref"] else "ax-tree ok; checkin button NOT visible (try CDP)",
                data,
            )
        if chosen == "cdp":
            return ActionResult(True, "desktop.probe", f"CDP port {self.cdp_port} reachable", data)
        data["snapshot"] = "no background backend available"
        return ActionResult(
            False, "desktop.probe",
            "install agent-desktop + grant Accessibility, or relaunch WorkBuddy with --remote-debugging-port=9222",
            data,
        )

    # ---- 每日签到 -----------------------------------------------------------
    def claim_daily(self, texts: list[str]) -> ActionResult:
        chosen = self._choose()
        if chosen is None:
            return ActionResult(False, "desktop.checkin", "no backend available", {"backend": None})
        if chosen == "agent-desktop":
            ok, message, payload = self._ax_snapshot()
            if not ok:
                return ActionResult(False, "desktop.checkin", message, {"backend": chosen})
            ref = find_ref(payload, texts)
            if ref is None:
                return ActionResult(
                    False, "desktop.checkin",
                    "checkin button not found in accessibility tree",
                    {"backend": chosen, "snapshot": str(payload)[:2000]},
                )
            ok, message, _data = self._exec_json([self.bin, "click", ref, "--session", self.session])
            return ActionResult(
                ok, "desktop.checkin", message, {"backend": chosen, "ref": ref}
            )
        if chosen == "cdp":
            js = SHADOW_CLICK_JS % json.dumps(texts, ensure_ascii=False)
            ok, out = self._exec(["agent-browser", "--cdp", str(self.cdp_port), "eval", js])
            return ActionResult(
                ok and "NOT_FOUND" not in out,
                "desktop.checkin", out[:800], {"backend": chosen},
            )
        return ActionResult(
            False, "desktop.checkin",
            "computer-use backend is disabled: it would move the physical mouse",
            {"backend": chosen},
        )


CUA_DRIVER_BIN = "/Applications/CuaDriver.app/Contents/MacOS/cua-driver"


class CuaDriverSigner(DesktopAdapter):
    """通过已授权的 CuaDriver (com.trycua.driver) 用 AX 路径点击签到按钮。

    实测 2026-11-09：WorkBuddy 客户端 AX 树的 Buddy 加油站卡片里含
    AXButton "立即领取"；用 element_index 点击走 AX press，
    不移动光标、不抢焦点，不干扰人工操作。点击后卡片与按钮消失即签到成功。
    """

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
