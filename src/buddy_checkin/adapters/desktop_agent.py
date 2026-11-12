"""桌面端签到适配器 · agent-desktop 引擎（严格 headless，不与人争抢）。

与 CuaDriverSigner 并列的第二引擎，由 ``desktop.engine`` 选择。核心承诺：

* **绝不抢焦点/光标**：所有动作走 agent-desktop 的 **ref 语义路径**（AX press），
  默认 strict headless delivery，不加 ``--headed``、不用 ``--xy`` 像素点击、
  不移动鼠标、不激活窗口。失败时**不**降级到 headed（agent-desktop 官方红线）。
* **登录不硬解**：检测到扫码/密码/短信登录或人机验证，自动全屏截图落盘、
  写状态日志、按配置等待人工登录（或提示拉起有头浏览器/复用已登录会话），
  绝不自动填账号密码、不绕验证码（遵循用户跨项目「人机验证换路不硬解」规则）。
* **重试有界**：只按 CLI 返回的 ``error.disposition.retry == 'safe'`` 重试，
  其余情况一律重新 snapshot 读实时状态再决定，避免重复点击造成二次提交。

CLI 契约（agent-desktop 1.2.0，obs+act+verify loop）：
  每条命令输出 JSON envelope {"version","ok","command","data"|"error"}。
  snapshot 给出全限定 ref ``@<snapshot_id>:eN``，UI 变化后即失效，必须重新取。
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .base import ActionResult, DesktopAdapter

DEFAULT_BIN = "agent-desktop"

# 登录/验证页强特征：命中即判「需人工」，不自动处理。
LOGIN_MARKERS = (
    "微信扫码登录", "扫码登录", "立即登录", "账号密码登录", "短信登录",
    "验证码", "滑动验证", "人机验证", "wechat-qrcode", "登录以继续",
)
# 已签到强文案（面板内）；与 CuaDriver 引擎保持一致
ALREADY_LABELS = ("今日已领取", "已领取", "明日再来", "今日已领", "已签到")
CHECKIN_LABELS = ("立即领取", "今日礼包", "签到领取", "领取今日礼包")
STATION_LABELS = ("Buddy加油站", "做任务赢积分", "每日可领")


def _walk_labels(node: Any, interactive_only: bool = False) -> list[tuple[str, str]]:
    """从 agent-desktop 结构化 AX 树里抽 (ref, 可见文案)。

    兼容两种返回：snapshot 的 data.tree/data.nodes（结构化），或退化的 data.text。
    """
    out: list[tuple[str, str]] = []
    stack = [node]

    def visible(d: dict) -> str:
        return " ".join(
            str(d.get(k) or "") for k in
            ("name", "label", "title", "value", "text", "description", "role_description")
        ).strip()

    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(cur)
        elif isinstance(cur, dict):
            ref = cur.get("ref")
            role = str(cur.get("role") or "")
            text = visible(cur)
            if isinstance(ref, str) and ref.startswith("@") and text:
                if not interactive_only or role.lower() in (
                        "button", "menuitem", "checkbox", "radiobutton", "link", "combobox"):
                    out.append((ref, text))
            for v in cur.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
    return out


def _find_ref(node: Any, keywords: tuple[str, ...],
              interactive_only: bool = True) -> tuple[str, str] | None:
    for ref, text in _walk_labels(node, interactive_only=interactive_only):
        if any(k in text for k in keywords):
            return ref, text
    return None


class AgentDesktopSigner(DesktopAdapter):
    """agent-desktop 引擎的桌面签到器（严格 headless，不抢焦点/光标）。"""

    def __init__(self, cfg: dict, runner: Callable[..., Any] | None = None,
                 screenshot_dir: str | Path | None = None) -> None:
        desk = cfg.get("desktop", {}) or {}
        bin_cfg = desk.get("agent_desktop_bin") or DEFAULT_BIN
        # 相对路径：先按项目 tools/ 解析（本地 npm 安装），否则交给 PATH
        bp = Path(bin_cfg).expanduser()
        if not bp.is_absolute():
            project_root = Path(__file__).resolve().parents[2]
            local = project_root / "tools" / bin_cfg
            self.bin = str(local) if local.exists() else bin_cfg
        else:
            self.bin = str(bp)
        self.app = desk.get("app", "WorkBuddy")
        self.session = desk.get("agent_desktop_session") or "buddy-checkin"
        self.login_wait_sec = float(desk.get("login_wait_sec", 0))  # 0=不阻塞，仅截图+报告
        self.screenshot_dir = Path(screenshot_dir or desk.get(
            "screenshot_dir", "~/.local/share/buddy-web-checkin/screenshots")).expanduser()
        self.runner = runner or subprocess.run
        self.commands: list[list[str]] = []
        self._last_tree: Any = None
        self._block: tuple[str, str] | None = None

    # ---- CLI ---------------------------------------------------------------
    def _run(self, *args: str, timeout: int = 40) -> dict:
        cmd = [self.bin, *args]
        self.commands.append(cmd)
        try:
            proc = self.runner(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "error": {"code": "EXEC_FAILED", "message": str(exc)}}
        raw = (getattr(proc, "stdout", "") or "").strip()
        err = (getattr(proc, "stderr", "") or "").strip()
        payload = raw or err
        i = payload.find("{")
        if i < 0:
            return {"ok": False, "error": {"code": "BAD_OUTPUT", "message": payload[:300]}}
        try:
            return json.loads(payload[i:])
        except ValueError:
            return {"ok": False, "error": {"code": "BAD_JSON", "message": payload[:300]}}

    def _snapshot(self, compact: bool = True) -> dict:
        args = ["snapshot", "--app", self.app, "--session", self.session]
        if compact:
            args.append("-i")
        args.append("--compact")
        return self._run(*args)

    def _data(self, env: dict) -> dict:
        d = env.get("data") or {}
        return d if isinstance(d, dict) else {}

    def _tree(self, env: dict) -> Any:
        d = self._data(env)
        for key in ("tree", "nodes", "root", "elements", "outline"):
            if key in d:
                return d[key]
        # 部分版本把扁平节点放在 data 下
        return d

    def _error_code(self, env: dict) -> str | None:
        err = env.get("error") or {}
        return err.get("code") if isinstance(err, dict) else None

    def _retry_safe(self, env: dict) -> bool:
        err = env.get("error")
        if isinstance(err, dict) and err.get("disposition", {}).get("retry") == "safe":
            return True
        data = env.get("data")
        return isinstance(data, dict) and data.get("disposition", {}).get("retry") == "safe"

    # ---- 登录检测 / 截图 / 等待 --------------------------------------------
    def _all_labels(self) -> list[str]:
        return [t for _r, t in _walk_labels(self._last_tree, interactive_only=False)]

    def _needs_login(self) -> bool:
        text = " ".join(self._all_labels())
        return any(m in text for m in LOGIN_MARKERS)

    def take_screenshot(self, tag: str = "login") -> str | None:
        """自动截图落盘（agent-desktop screenshot；失败则 CuaDriver 全屏兜底）。"""
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        path = self.screenshot_dir / f"{tag}-{int(time.time())}.png"
        env = self._run("screenshot", "--app", self.app, "--path", str(path))
        if env.get("ok"):
            return str(path)
        # 兜底：CuaDriver 全屏（已授权 com.trycua.driver），独立身份不依赖 iTerm 授权
        try:
            bin_ = "/Applications/CuaDriver.app/Contents/MacOS/cua-driver"
            subprocess.run([bin_, "call", "start_session", json.dumps({
                "session": "buddy-checkin-shot", "capture_scope": "desktop"})],
                capture_output=True, text=True, timeout=15)
            r = subprocess.run([bin_, "call", "get_desktop_state", json.dumps({
                "session": "buddy-checkin-shot", "screenshot_out_file": str(path)})],
                capture_output=True, text=True, timeout=20)
            return str(path) if path.exists() and r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    def _login_result(self) -> ActionResult:
        shot = self.take_screenshot("login-required")
        detail = "登录/验证页检测到（不自动登录、不绕验证码）"
        if shot:
            detail += f"；截图: {shot}"
        if self.login_wait_sec > 0:
            detail += f"；等待人工最多 {int(self.login_wait_sec)}s"
            deadline = time.time() + self.login_wait_sec
            while time.time() < deadline:
                time.sleep(min(3, max(0.5, deadline - time.time())))
                env = self._snapshot()
                if env.get("ok"):
                    self._last_tree = self._tree(env)
                    if not self._needs_login():
                        return ActionResult(True, "desktop.checkin",
                                            "登录后已恢复，可继续签到",
                                            {"login_resolved": True, "screenshot": shot})
        return ActionResult(False, "desktop.checkin", detail, {
            "block": "login_required", "screenshot": shot, "confirmed": False,
        })

    # ---- DesktopAdapter 契约 ------------------------------------------------
    def probe(self) -> ActionResult:
        env = self._snapshot()
        if not env.get("ok"):
            code = self._error_code(env)
            msg = (env.get("error") or {}).get("message", str(env.get("error", ""))) \
                if isinstance(env.get("error"), dict) else str(env.get("error", ""))
            return ActionResult(False, "desktop.probe", f"{code}: {msg}",
                                {"backend": "agent-desktop", "error_code": code})
        self._last_tree = self._tree(env)
        return ActionResult(True, "desktop.probe", "snapshot ok",
                            {"backend": "agent-desktop", "needs_login": self._needs_login()})

    def claim_daily(self, texts: list[str]) -> ActionResult:
        """签到一轮（strict headless）：先展开加油站，再点领取并复核。

        任何需要登录/验证或权限/策略拒绝的情况都安全返回，不 headed 降级。
        """
        for attempt in range(3):
            env = self._snapshot()
            if not env.get("ok"):
                code = self._error_code(env)
                if code in ("PERM_DENIED", "AUTHORIZATION", "POLICY_DENIED"):
                    msg = (env.get("error") or {}).get("message", code) \
                        if isinstance(env.get("error"), dict) else code
                    return ActionResult(False, "desktop.checkin",
                                        f"agent-desktop 被拒({code}): {msg}；"
                                        "请给启动它的 App 授予辅助功能权限，或切回 engine=ax",
                                        {"backend": "agent-desktop", "block": "permission",
                                         "error_code": code, "confirmed": False})
                if not self._retry_safe(env):
                    return ActionResult(False, "desktop.checkin",
                                        f"snapshot failed: {code}",
                                        {"backend": "agent-desktop", "confirmed": False})
                time.sleep(0.8)
                continue

            self._last_tree = self._tree(env)
            if self._needs_login():
                return self._login_result()

            station = _find_ref(self._last_tree, STATION_LABELS, interactive_only=True)
            btn = _find_ref(self._last_tree, tuple(texts), interactive_only=True)
            expanded = False
            if btn is None and station is not None:
                clicked = self._run("click", station[0], "--session", self.session)
                if not clicked.get("ok"):
                    if not self._retry_safe(clicked):
                        return ActionResult(False, "desktop.checkin",
                                            f"open panel failed: {self._error_code(clicked)}",
                                            {"backend": "agent-desktop", "confirmed": False})
                    time.sleep(0.8)
                    continue
                expanded = True
                env = self._snapshot()
                if not env.get("ok"):
                    continue
                self._last_tree = self._tree(env)
                btn = _find_ref(self._last_tree, tuple(texts), interactive_only=True)

            if btn is None:
                already = _find_ref(self._last_tree, ALREADY_LABELS, interactive_only=True)
                if already is not None:
                    return ActionResult(True, "desktop.checkin",
                                        f"already checked in today ({already[1]})",
                                        {"backend": "agent-desktop", "clicked": False,
                                         "already": True, "confirmed": True,
                                         "evidence": already[1]})
                if expanded:
                    return ActionResult(True, "desktop.checkin",
                                        "station panel open, no checkin button; already checked in",
                                        {"backend": "agent-desktop", "clicked": False,
                                         "already": True, "confirmed": True,
                                         "station_expanded": True})
                return ActionResult(False, "desktop.checkin",
                                    "checkin button and station entry both missing; uncertain",
                                    {"backend": "agent-desktop", "confirmed": False,
                                     "uncertain": True})

            clicked = self._run("click", btn[0], "--session", self.session)
            if not clicked.get("ok"):
                if not self._retry_safe(clicked):
                    return ActionResult(False, "desktop.checkin",
                                        f"click failed: {self._error_code(clicked)}",
                                        {"backend": "agent-desktop", "confirmed": False})
                time.sleep(0.8)
                continue
            # verify：重新 snapshot 同一区域，按钮消失即成功
            verify = self._snapshot()
            if verify.get("ok"):
                self._last_tree = self._tree(verify)
                still = _find_ref(self._last_tree, tuple(texts), interactive_only=True)
                if still is not None:
                    time.sleep(0.8)
                    continue
                return ActionResult(True, "desktop.checkin",
                                    "clicked and button disappeared (checkin verified)",
                                    {"backend": "agent-desktop", "clicked": True,
                                     "verified": True, "confirmed": True,
                                     "station_expanded": expanded})
            return ActionResult(True, "desktop.checkin", "clicked (verify snapshot failed)",
                                {"backend": "agent-desktop", "clicked": True,
                                 "confirmed": True, "station_expanded": expanded})
        return ActionResult(False, "desktop.checkin", "checkin not confirmed after retries",
                            {"backend": "agent-desktop", "confirmed": False})
