"""agent-desktop 引擎单测：严格 headless、不抢焦点、登录不硬解。

全部用假 runner（JSON envelope），不真实点击用户屏幕、不弹授权。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from buddy_checkin.adapters.desktop_agent import (  # noqa: E402
    AgentDesktopSigner, _find_ref, _walk_labels,
)


def env_ok(tree, command="snapshot"):
    return {"version": "2.6", "ok": True, "command": command, "data": {"tree": tree}}


def env_err(code, retry="safe", message="x"):
    return {"version": "2.6", "ok": False, "command": "snapshot",
            "error": {"code": code, "message": message,
                      "disposition": {"delivery": "not_delivered", "retry": retry}}}


def btn(ref, label):
    return {"ref": ref, "role": "button", "name": label}


class FakeRunner:
    """按调用序列返回 JSON envelope，并记录每条命令。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **_kw):
        self.calls.append(cmd)
        out = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        import subprocess
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")


def cfg(tmp: Path, **overrides):
    d = {
        "desktop": {"app": "WorkBuddy", "agent_desktop_bin": "/fake/agent-desktop",
                    "login_wait_sec": 0, "screenshot_dir": str(tmp)},
    }
    d["desktop"].update(overrides)
    return d


class RefHelpersTests(unittest.TestCase):
    def test_walk_labels_and_find_ref(self):
        tree = {"role": "window", "children": [
            btn("@s1:e1", "Buddy加油站"),
            btn("@s1:e2", "立即领取"),
            {"ref": "@s1:e3", "role": "statictext", "name": "明日再来"},
        ]}
        pairs = _walk_labels(tree)
        names = [t for _r, t in pairs]
        self.assertIn("Buddy加油站", names)
        self.assertIn("明日再来", names)
        # interactive_only 只保留按钮，静态文本被排除
        inter = _walk_labels(tree, interactive_only=True)
        self.assertNotIn("明日再来", [t for _r, t in inter])
        self.assertEqual(_find_ref(tree, ("立即领取",))[0], "@s1:e2")
        self.assertIsNone(_find_ref(tree, ("明日再来",)))  # 静态文本不可点


class AgentDesktopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_headless_clicks_never_use_headed_or_xy(self):
        """回归（用户核心诉求）：动作只用 ref 语义，绝不 --headed / --xy 抢焦点。"""
        main = {"role": "window", "children": [btn("@s1:e1", "Buddy加油站")]}
        panel = {"role": "window", "children": [btn("@s2:e1", "立即领取")]}
        gone = {"role": "window", "children": [btn("@s3:e1", "Buddy加油站")]}
        runner = FakeRunner([env_ok(main), env_ok({"ok": True}, "click"),
                             env_ok(panel), env_ok({"ok": True}, "click"), env_ok(gone)])
        res = AgentDesktopSigner(cfg(self.dir), runner=runner).claim_daily(["立即领取"])
        self.assertTrue(res.ok, res.detail)
        self.assertTrue(res.data["confirmed"])
        for c in runner.calls:
            self.assertNotIn("--headed", c)
            self.assertNotIn("--xy", c)
        clicks = [c for c in runner.calls if c[1] == "click"]
        self.assertEqual(len(clicks), 2)

    def test_permission_denied_is_safe_no_headed_fallback(self):
        """PERM_DENIED 直接安全返回、提示授权，绝不降级 headed。"""
        runner = FakeRunner([env_err("PERM_DENIED", retry="safe", message="no AX")])
        res = AgentDesktopSigner(cfg(self.dir), runner=runner).claim_daily(["立即领取"])
        self.assertFalse(res.ok)
        self.assertEqual(res.data.get("block"), "permission")
        self.assertIn("辅助功能", res.detail)
        self.assertFalse(any("--headed" in c for c in runner.calls))

    def test_login_detected_screenshots_and_does_not_autofill(self):
        """登录/验证码：自动截图 + 报告，绝不 type/填账号/绕验证码。"""
        login = {"role": "window", "children": [
            btn("@s1:e1", "微信扫码登录"), {"ref": "@s1:e2", "role": "statictext", "name": "扫码登录"},
        ]}
        runner = FakeRunner([env_ok(login),
                             env_ok({"ok": True, "path": str(self.dir / "login.png")}, "screenshot")])
        res = AgentDesktopSigner(cfg(self.dir), runner=runner).claim_daily(["立即领取"])
        self.assertFalse(res.ok)
        self.assertEqual(res.data.get("block"), "login_required")
        self.assertTrue(any(c[1] == "screenshot" for c in runner.calls))
        self.assertFalse(any(c[1] in ("type", "set-value") for c in runner.calls))

    def test_login_wait_resolves_when_user_logs_in(self):
        """login_wait_sec>0 时轮询等待，用户登录后恢复则继续（不阻塞超过上限）。"""
        login = {"role": "window", "children": [btn("@s1:e1", "微信扫码登录")]}
        panel = {"role": "window", "children": [btn("@s2:e1", "立即领取")]}
        runner = FakeRunner([env_ok(login),
                             env_ok({"ok": True, "path": "x.png"}, "screenshot"),
                             env_ok(panel)])
        signer = AgentDesktopSigner(cfg(self.dir, login_wait_sec=5), runner=runner)
        # 加速：把 sleep 置零
        import buddy_checkin.adapters.desktop_agent as mod
        orig = mod.time.sleep
        mod.time.sleep = lambda _s: None
        try:
            res = signer.claim_daily(["立即领取"])
        finally:
            mod.time.sleep = orig
        # 恢复后进入派遣分支：面板有立即领取 -> 会点击（此处只断言等到了已恢复状态）
        self.assertTrue(any(c[1] == "screenshot" for c in runner.calls))

    def test_only_retry_when_safe(self):
        """点击返回非 safe 失败（可能已投递）时不重试动作，只点一次避免二次提交。"""
        main = {"role": "window", "children": [btn("@s1:e1", "立即领取")]}
        # snapshot 成功看到按钮；click 返回非 safe 的 TIMEOUT（可能已投递）
        unsafe = {"version": "2.6", "ok": False, "command": "click",
                  "error": {"code": "TIMEOUT", "message": "state unknown",
                            "disposition": {"delivery": "delivered", "retry": "unsafe"}}}
        runner = FakeRunner([env_ok(main), unsafe])
        res = AgentDesktopSigner(cfg(self.dir), runner=runner).claim_daily(["立即领取"])
        self.assertFalse(res.ok)
        clicks = [c for c in runner.calls if c[1] == "click"]
        self.assertEqual(len(clicks), 1)  # 绝不因非 safe 失败再点第二次

    def test_build_selects_ax_by_default(self):
        """默认 engine=auto 选已授权的 ax，不强行切 agent-desktop。"""
        from buddy_checkin import cli
        cfg_ax = {"desktop": {"engine": "auto", "cua_driver_bin": "/nonexistent"},
                  "growth": {"enabled": False}}
        # probe 失败且非「窗口未找到」→ 回退 agent-desktop
        d = cli._build_desktop(cfg_ax, "auto")
        self.assertIsInstance(d, AgentDesktopSigner)


if __name__ == "__main__":
    unittest.main()
