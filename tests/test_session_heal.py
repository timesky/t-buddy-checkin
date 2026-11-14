"""daemon 长驻会话失效后的自愈（2026-11-14 线上事故）。

事故经过：客户端从 5.11.2 静默升级到 5.11.3 并重启，daemon 里跑了 46 小时的
agent-browser 会话仍指向旧窗口，之后 8 小时每轮都是
`no growth entry found in client`（27 次），自己不会恢复；手动重启进程立刻正常。

三条约束：
  1. 会话可疑时先重连一次并重试一轮；
  2. 失败原因不能吞掉（要能看出是快照没按钮、还是点了没反应）；
  3. 连续多轮连客户端窗口都看不到 → 退出进程，交给 launchd 重建会话。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from buddy_checkin.adapters.base import ActionResult  # noqa: E402
from buddy_checkin.adapters.growth_browser import GrowthBrowser  # noqa: E402


def _cfg(**growth) -> dict:
    g = {"enabled": True, "cdp_port": 9222, "open_labels": ["做任务赢积分"]}
    g.update(growth)
    return {"growth": g}


class _Base(unittest.TestCase):
    def setUp(self):
        p = patch("buddy_checkin.adapters.growth_browser.time.sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)
        self.gb = GrowthBrowser(_cfg())
        self.gb._iframe_ws = lambda: None
        self.gb.available = lambda: True
        self.gb._wait_page_ready = lambda timeout=10.0: True
        self.ab_calls: list[tuple] = []

    def fake_ab(self, *args, timeout=60):
        self.ab_calls.append(args)
        return 0, ""


class ReconnectRetryTests(_Base):
    def test_reconnects_once_then_succeeds(self):
        """第一轮点不中入口 → 重连 → 第二轮点中，整体算成功。"""
        state = {"iframes": 0, "clicks": 0}
        self.gb._ensure_connected = lambda attempts=2: True
        self.gb._ab = self.fake_ab

        def click(label, attempts=3):
            if label != "成长计划":
                return False, "no such button"
            state["clicks"] += 1
            if state["clicks"] == 1:
                return False, 'snapshot has no button "成长计划"'
            state["iframes"] = 1
            return True, label

        self.gb._click_label = click
        self.gb._iframe_ws = lambda: "ws://x" if state["iframes"] else None

        r = self.gb.ensure_page()
        self.assertTrue(r.ok, r.detail)
        self.assertEqual(1, state["iframes"])
        self.assertEqual(2, state["clicks"])
        self.assertIn(("connect", "9222"), self.ab_calls, "重试前应重连 CDP")
        self.assertTrue(self.gb._opened_this_run)

    def test_failure_keeps_per_label_reasons(self):
        """两次都失败时要能看到每个入口的真实原因，而不是一句笼统的话。"""
        self.gb._ensure_connected = lambda attempts=2: True
        self.gb._ab = self.fake_ab
        self.gb._click_label = lambda label, attempts=3: (
            False, f'snapshot has no button "{label}"')
        self.gb._looks_like_client = lambda: True      # 客户端在，别触发重启

        r = self.gb.ensure_page()
        self.assertFalse(r.ok)
        self.assertIn("no growth entry found", r.detail)
        self.assertIn('"成长计划"', r.detail)
        self.assertIn("做任务赢积分", r.detail)


class SelfHealTests(_Base):
    def test_no_heal_when_client_visible(self):
        self.gb._looks_like_client = lambda: True
        self.gb._stale_hits = 2
        self.gb._maybe_self_heal()
        self.assertEqual(0, self.gb._stale_hits, "看到客户端窗口就应清零")

    def test_accumulates_when_client_invisible(self):
        self.gb._looks_like_client = lambda: False
        self.gb._maybe_self_heal()
        self.gb._maybe_self_heal()
        self.assertEqual(2, self.gb._stale_hits)

    def test_third_consecutive_stale_exits_for_launchd_restart(self):
        self.gb._looks_like_client = lambda: False
        with self.assertRaises(SystemExit) as ctx:
            for _ in range(3):
                self.gb._maybe_self_heal()
        self.assertEqual(75, ctx.exception.code)

    def test_success_resets_counter(self):
        state = {"iframes": 0}
        self.gb._ensure_connected = lambda attempts=2: True
        self.gb._ab = self.fake_ab
        self.gb._stale_hits = 2

        def click(label, attempts=3):
            if label == "成长计划":
                state["iframes"] = 1
                return True, label
            return False, "no such button"

        self.gb._click_label = click
        self.gb._iframe_ws = lambda: "ws://x" if state["iframes"] else None
        r = self.gb.ensure_page()
        self.assertTrue(r.ok)
        self.assertEqual(0, self.gb._stale_hits, "成功打开后失效计数必须清零")


if __name__ == "__main__":
    unittest.main()
