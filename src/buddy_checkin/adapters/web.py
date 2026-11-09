"""网页端适配器：agent-browser CLI，headless + 独立 session + 独立 profile。

关键点：默认 headless，且 --session/--profile 与用户日常浏览器完全隔离，
因此定时任务跑起来时不会弹窗、不会抢焦点、不会碰用户 Chrome 的登录态。
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from ..config import SENSITIVE_KEYS, profile_path
from .base import ActionResult, WebAdapter


def scrub_text(text: str) -> str:
    """命令行输出按行脱敏，任何凭据都不落盘。"""
    lines: list[str] = []
    for line in (text or "").splitlines():
        if any(key in line.lower() for key in SENSITIVE_KEYS):
            lines.append("<redacted>")
        else:
            lines.append(line[:500])
    return "\n".join(lines)


class AgentBrowser(WebAdapter):
    def __init__(self, cfg: dict, runner: Callable[..., Any] | None = None) -> None:
        web = cfg["web"]
        self.bin = web["bin"]
        self.session = web["session"]
        self.profile = str(profile_path(cfg))
        self.executable_path = str(web.get("executable_path", "") or "")
        self.headless = bool(web["headless"])
        self.timeout = int(web["timeout_sec"])
        self.base_url = cfg["base_url"]
        self.runner = runner or subprocess.run
        Path(self.profile).mkdir(parents=True, exist_ok=True)
        self.commands: list[list[str]] = []

    def build(self, args: list[str]) -> list[str]:
        cmd = [self.bin, "--session", self.session, "--profile", self.profile]
        if self.executable_path:
            cmd += ["--executable-path", self.executable_path]
        if not self.headless:
            cmd.append("--headed")
        return cmd + args

    def cold_restart(self) -> None:
        """daemon 单例：换 executable/profile 前必须彻底关掉，否则参数被忽略。"""
        self.runner(
            [self.bin, "close", "--all"],
            capture_output=True, text=True, timeout=self.timeout,
        )

    def _run(self, args: list[str]) -> tuple[bool, str]:
        cmd = self.build(args)
        self.commands.append(cmd)
        try:
            proc = self.runner(cmd, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"command failed: {exc}"
        out = scrub_text(getattr(proc, "stdout", "") or "")
        err = scrub_text(getattr(proc, "stderr", "") or "")
        ok = getattr(proc, "returncode", 1) == 0
        return ok, out if ok else (out + "\n" + err).strip()

    def open(self, url: str | None = None) -> ActionResult:
        ok, out = self._run(["open", url or self.base_url])
        return ActionResult(ok, "web.open", out[-500:])

    def snapshot(self) -> str:
        _ok, out = self._run(["snapshot", "-i"])
        return out

    def eval_js(self, js: str) -> str:
        _ok, out = self._run(["eval", js])
        return out

    def get_url(self) -> str:
        _ok, out = self._run(["get", "url"])
        return out.strip()

    def screenshot(self, path: str) -> str:
        ok, out = self._run(["screenshot", path])
        return path if ok else out

    def wait(self, ms: int = 3000) -> None:
        self._run(["wait", str(ms)])

    def wait_for_login_page(self, timeout_sec: int = 20) -> bool:
        """等二维码 iframe 渲染完再截图。实测：立刻截图只能拿到骨架屏 + spinner。"""
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            snap = self.snapshot()
            if "wechat-qrcode" in snap or "微信扫码登录" in snap:
                return True
            self.wait(2000)
        return False

    def click_text(self, texts: list[str]) -> bool:
        for text in texts:
            ok, _out = self._run(["find", "text", text, "click"])
            if ok:
                return True
        return False

    def close(self) -> None:
        self._run(["close"])

    def probe(self) -> dict[str, Any]:
        """只读探测：等重定向与渲染稳定后再记录 URL 和控件，不做任何点击。

        实测：open 后立刻取 URL 会读成 /app，5 秒后才真正落到 /login。
        """
        opened = self.open()
        self.wait(5000)
        return {
            "engine": "agent-browser",
            "session": self.session,
            "profile": self.profile,
            "headless": self.headless,
            "open_ok": opened.ok,
            "url": self.get_url(),
            "snapshot": self.snapshot(),
        }
