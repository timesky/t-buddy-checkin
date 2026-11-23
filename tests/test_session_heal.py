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


class IframeMatchTests(unittest.TestCase):
    """_iframe_ws 只认 growth-center 路径（2026-11-19 文档页误认事故）。"""

    def _gb(self):
        p = patch("buddy_checkin.adapters.growth_browser.time.sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)
        gb = GrowthBrowser({"growth": {"open_labels": ["成长计划"]}})
        return gb

    def test_doc_page_iframe_is_never_mistaken_for_growth_center(self):
        gb = self._gb()
        gb.targets = lambda: [
            {"type": "page", "url": "file:///Applications/WorkBuddy.app/...",
             "webSocketDebuggerUrl": "ws://main"},
            # 用户打开的文档页：workbuddy.cn 域但不是成长中心
            {"type": "iframe", "url": "https://www.workbuddy.cn/space/d/VHGbO0BDbHY19ANxFkgwNA",
             "webSocketDebuggerUrl": "ws://doc"},
        ]
        self.assertIsNone(gb._iframe_ws(),
                          "文档页 iframe 绝不能被当成成长中心")

    def test_growth_center_wins_over_doc_page(self):
        gb = self._gb()
        gb.targets = lambda: [
            {"type": "iframe", "url": "https://www.workbuddy.cn/space/d/xxx",
             "webSocketDebuggerUrl": "ws://doc"},
            {"type": "iframe", "url": "https://www.workbuddy.cn/profile/growth-center?x=1",
             "webSocketDebuggerUrl": "ws://growth"},
        ]
        self.assertEqual(gb._iframe_ws(), "ws://growth")


class _Base(unittest.TestCase):
    def setUp(self):
        p = patch("buddy_checkin.adapters.growth_browser.time.sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)
        self.gb = GrowthBrowser(_cfg())
        self.gb._iframe_ws = lambda: None
        self.gb.available = lambda: True
        self.gb._wait_page_ready = lambda timeout=10.0: True
        # 默认不触发跨天刷新（由 FreshnessTests 单独覆盖）
        self.reload_calls: list[str] = []
        self.gb._refresh_if_stale = lambda timeout=30.0: False
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


class FuelCardEntryFallbackTests(_Base):
    """2026-11-16 另一台机器：侧栏入口不在，浮层运营卡直接挂着「立即前往」。"""

    def test_direct_growth_button_opens_page(self):
        self.gb._ensure_connected = lambda attempts=2: True
        self.gb._ab = self.fake_ab
        self.gb._looks_like_client = lambda: True
        clicks: list[str] = []
        state = {"iframe": False}

        def click(label, attempts=3):
            clicks.append(label)
            if label == "立即前往":
                state["iframe"] = True
                return True, label
            return False, "snapshot has no button"

        self.gb._click_label = click
        self.gb._iframe_ws = lambda: "ws://x" if state["iframe"] else None
        r = self.gb.ensure_page()
        self.assertTrue(r.ok, r.detail)
        self.assertIn("立即前往", clicks)
        self.assertEqual(clicks.count("立即前往"), 1,
                         "浮层按钮本身就是最后一步，不该再点第二次")
        self.assertTrue(self.gb._opened_this_run)

    def test_fallback_only_after_configured_labels(self):
        """兜底必须排在常规入口之后，避免优先误点运营卡。"""
        self.gb._ensure_connected = lambda attempts=2: True
        self.gb._ab = self.fake_ab
        self.gb._looks_like_client = lambda: True
        clicks: list[str] = []
        self.gb._click_label = lambda label, attempts=3: (
            clicks.append(label) or (False, "snapshot has no button"))
        r = self.gb.ensure_page()
        self.assertFalse(r.ok)
        self.assertEqual(clicks[:2], ["成长计划", "做任务赢积分"])
        self.assertEqual(clicks[-1], "立即前往")
        self.assertIn("no growth entry found", r.detail)


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


class FreshnessTests(_Base):
    """跨天不刷新探测（2026-11-23 两机实际事故）。

    成长中心 SPA 跨天后不自动重取数据：is-today 仍指昨天的格子，
    makeup 整日报 missing_days=[昨天] 永远 pending，travel 读到昨天
    的「明天再来」误判派遣用完。发现 is-today 日号与真实日期不一致
    就必须 location.reload() 重载 iframe。
    """

    def setUp(self):
        super().setUp()
        # 摘掉 _Base 的 stub，测真正的 _refresh_if_stale
        del self.gb._refresh_if_stale

    def _install_clock(self, *, stale_first: bool):
        """FRESHNESS_JS 第一次返回 stale_first，reload 后返回 False。"""
        self.reloaded = 0

        def fake_eval(js, timeout=20):
            if "location.reload" in js:
                self.reloaded += 1
                return True, "reloading"
            if "is-today" in js:
                stale = stale_first and self.reloaded == 0
                return True, {"stale": stale, "today_text": "22" if stale else "23"}
            return False, "unexpected js"

        self.gb._eval = fake_eval

    def test_refresh_if_stale_reloads_and_recovers(self):
        self._install_clock(stale_first=True)
        self.assertTrue(self.gb._refresh_if_stale())
        self.assertEqual(1, self.reloaded, "发现旧渲染必须重载一次")

    def test_fresh_page_never_reloaded(self):
        self._install_clock(stale_first=False)
        self.assertFalse(self.gb._refresh_if_stale())
        self.assertEqual(0, self.reloaded, "页面新鲜时绝不能重载")

    def test_ensure_page_refreshes_stale_page_before_ready(self):
        """任务入口拿到旧渲染页面时必须先刷新再继续（travel/makeup 都走这里）。"""
        self.gb._iframe_ws = lambda: "ws://growth"
        self._install_clock(stale_first=True)
        self.gb._wait_page_ready = lambda timeout=10.0: True
        r = self.gb.ensure_page()
        self.assertTrue(r.ok, r.detail)
        self.assertEqual(1, self.reloaded, "ensure_page 应先刷新旧渲染")

    def test_calendar_scan_rescans_after_stale_refresh(self):
        """calendar_scan 探到 stale 后重载并重扫一轮。"""
        stale_payload = {"ok": True, "month": "2026-11", "today": "2026-11-23",
                         "day_of_month": 23, "stale": True, "cells": [],
                         "card_text": "", "card_balance": None,
                         "streak_days": 23, "today_cell": None,
                         "render_ok": False, "render_reason": "no_today_cell"}
        fresh_cells = [{"day": d, "date": f"2026-11-{d:02d}", "checked": True,
                        "future": False, "clickable": False, "text": str(d)}
                       for d in range(1, 24)]
        fresh_payload = dict(stale_payload, stale=False,
                             cells=fresh_cells, render_ok=True,
                             render_reason="",
                             today_cell={"date": "2026-11-23", "checked": True,
                                         "day": 23, "alpha": 1.0})
        payloads = [stale_payload, fresh_payload]

        def fake_eval(js, timeout=20):
            if "gs-cal-cell" in js:
                return True, payloads.pop(0)
            return True, None

        self.gb._eval = fake_eval
        self.gb._refresh_if_stale = lambda timeout=30.0: True
        scan = self.gb.calendar_scan()
        self.assertTrue(scan["ok"])
        self.assertTrue(scan["render_ok"], "重扫后必须用新数据")
        self.assertTrue(scan["today_checked"])


if __name__ == "__main__":
    unittest.main()
