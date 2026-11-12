"""两个已复现缺陷的回归测试（2026-11-12）：

1. 兑换误点礼物：兑换弹层未打开时，主面板的「领取礼物」留在 DOM 且可用，
   旧过滤（含 领取|兑换）会把它当里程碑奖励点掉，误触发旅行领奖。
2. 端口归属：cdp_port 被 Chrome 等其它浏览器占用时，旧实现把「CDP 可达」
   当成可用，agent-browser 会连到错误的浏览器，排查只剩一句
   "iframe never appeared"。现在必须明确判定端口属于谁。
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from buddy_checkin.adapters.base import ActionResult  # noqa: E402
from buddy_checkin.adapters.growth_browser import (  # noqa: E402
    GrowthBrowser,
    is_exchange_reward,
)

# 5.11.2 主面板实测按钮
PANEL_BUTTONS = [
    {"t": "派猫猫旅行", "dis": False},
    {"t": "领取礼物", "dis": False},
    {"t": "抽奖(2)", "dis": False},
    {"t": "开启盲盒 3/3", "dis": False},
    {"t": "兑换奖励", "dis": False},
]

# 只留里程碑相关按钮，避免抽奖/盲盒干扰 read_state 的调用顺序
MILESTONE_PANEL = [
    {"t": "派猫猫旅行", "dis": False},
    {"t": "领取礼物", "dis": False},
    {"t": "兑换奖励", "dis": False},
]


def _cfg(**growth) -> dict:
    g = {"enabled": True, "cdp_port": 9222}
    g.update(growth)
    return {"growth": g}


class ExchangeRewardLabelTests(unittest.TestCase):
    def test_panel_buttons_are_not_rewards(self):
        for label in ("领取礼物", "抽奖(2)", "开启盲盒 3/3", "派猫猫旅行",
                      "旅行倒计时 01:20:00", "兑换奖励", "领取", "确定派出"):
            self.assertFalse(is_exchange_reward(label), f"{label} 不应被判为兑换奖励")

    def test_real_reward_items_pass(self):
        """5.11.2 实测：里程碑奖励按钮文案就是纯「兑换」。"""
        for label in ("兑换", "领取 50 积分", "领取7积分", "领取连登3天奖励"):
            self.assertTrue(is_exchange_reward(label), f"{label} 应被判为兑换奖励")

    def test_claimed_and_locked_items_are_rejected(self):
        for label in ("已兑换", "已领取 50 积分", "还差 3 天", "本月已兑换", "补签 11-01"):
            self.assertFalse(is_exchange_reward(label))


class ExchangeDoesNotTouchGiftTests(unittest.TestCase):
    def setUp(self):
        # 动作里有 1.0~1.2s 的等待，测试里不需要
        p = patch("buddy_checkin.adapters.growth_browser.time.sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

    def test_milestone_without_dialog_never_clicks_gift(self):
        gb = GrowthBrowser(_cfg())
        gb.ensure_page = lambda: ActionResult(True, "growth_open", "open", {})
        gb._close_dialog = lambda: False
        gb.close_page = lambda attempts=2: True
        gb.read_state = lambda: {"state": "idle", "remaining_sec": None,
                                 "raw": "派猫猫旅行 领取礼物 兑换奖励",
                                 "buttons": MILESTONE_PANEL}
        clicked: list[str] = []

        def fake_click(text, *_a, **_k):
            clicked.append(text)
            return True, text

        gb._click_text = fake_click
        results = gb.claim_exchanges()

        self.assertIn("兑换奖励", clicked, "仍应尝试打开兑换弹层")
        self.assertNotIn("领取礼物", clicked, "绝不能把「领取礼物」当里程碑奖励点掉")
        self.assertNotIn("派猫猫旅行", clicked)
        self.assertTrue(all(r.ok for r in results))
        self.assertTrue(any("no unlocked reward" in r.detail for r in results))

    def test_lottery_and_blindbox_still_exchanged(self):
        """没弹层时抽奖/盲盒本来就是独立可兑项，不能被这次收紧误伤。"""
        gb = GrowthBrowser(_cfg())
        gb.ensure_page = lambda: ActionResult(True, "growth_open", "open", {})
        gb._close_dialog = lambda: False
        gb.close_page = lambda attempts=2: True
        gb.read_state = lambda: {"state": "idle", "remaining_sec": None,
                                 "raw": "抽奖(1) 开启盲盒 0/3",
                                 "buttons": PANEL_BUTTONS}
        clicked: list[str] = []

        def fake_click(text, *_a, **_k):
            clicked.append(text)
            return True, text

        gb._click_text = fake_click
        gb.claim_exchanges()
        self.assertIn("抽奖(2)", clicked)
        self.assertIn("开启盲盒 3/3", clicked)
        self.assertNotIn("领取礼物", clicked)

    def test_milestone_dialog_picks_real_reward(self):
        gb = GrowthBrowser(_cfg())
        gb.ensure_page = lambda: ActionResult(True, "growth_open", "open", {})
        gb._close_dialog = lambda: False
        gb.close_page = lambda attempts=2: True
        states = [
            {"state": "idle", "remaining_sec": None, "raw": "兑换奖励",
             "buttons": MILESTONE_PANEL},
            # 弹层打开：底层面板按钮仍在（这是坑），另加真奖励项
            {"state": "idle", "remaining_sec": None, "raw": "选择奖励 本月已兑换",
             "buttons": MILESTONE_PANEL + [{"t": "兑换", "dis": False,
                                            "aria": "兑换 +2 能量"}]},
        ]
        gb.read_state = lambda: states.pop(0) if states else {
            "state": "idle", "remaining_sec": None, "raw": "", "buttons": MILESTONE_PANEL}
        clicked: list[str] = []

        def fake_click(text, *_a, **_k):
            clicked.append(text)
            return True, text

        gb._click_text = fake_click
        results = gb.claim_exchanges()
        self.assertIn("兑换", clicked, "弹层里的真奖励项文案是「兑换」")
        self.assertNotIn("领取礼物", clicked)
        self.assertTrue(any("milestone claimed" in r.detail for r in results))

    def test_locked_reward_with_aria_is_skipped(self):
        """aria 写明「还差 N 天」时即便 disabled 属性缺失也不许点。"""
        gb = GrowthBrowser(_cfg())
        gb.ensure_page = lambda: ActionResult(True, "growth_open", "open", {})
        gb._close_dialog = lambda: False
        gb.close_page = lambda attempts=2: True
        states = [
            {"state": "idle", "remaining_sec": None, "raw": "兑换奖励",
             "buttons": MILESTONE_PANEL},
            {"state": "idle", "remaining_sec": None, "raw": "选择奖励",
             "buttons": MILESTONE_PANEL + [
                 {"t": "兑换", "dis": False, "aria": "还差 4 天解锁奖励"}]},
        ]
        gb.read_state = lambda: states.pop(0) if states else {
            "state": "idle", "remaining_sec": None, "raw": "", "buttons": MILESTONE_PANEL}
        clicked: list[str] = []

        def fake_click(text, *_a, **_k):
            clicked.append(text)
            return True, text

        gb._click_text = fake_click
        results = gb.claim_exchanges()
        self.assertNotIn("兑换", clicked, "还差 N 天的奖励不得点击")
        self.assertTrue(any("no unlocked reward" in r.detail for r in results))


class PortOwnershipTests(unittest.TestCase):
    def _fake_http(self, version: dict, targets: list[dict]):
        def fake(path: str):
            if path.startswith("/json/version"):
                return version
            return targets
        return fake

    def test_chrome_port_is_rejected(self):
        gb = GrowthBrowser(_cfg(cdp_port=9333))
        gb._http = self._fake_http(
            {"Browser": "Chrome/138.0.7204.251",
             "User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36"},
            [{"type": "page", "title": "Example Domain", "url": "https://example.com/"}],
        )
        self.assertFalse(gb.available())
        self.assertEqual("cdp_port_foreign", gb.block[0])
        self.assertIn("not WorkBuddy", gb.block[1])
        r = gb.ensure_page()
        self.assertFalse(r.ok)
        self.assertEqual("cdp_port_foreign", r.data.get("blocked"))

    def test_other_electron_app_is_rejected(self):
        gb = GrowthBrowser(_cfg(cdp_port=9334))
        gb._http = self._fake_http(
            {"Browser": "Chrome/138.0.7204.251",
             "User-Agent": "Mozilla/5.0 SomeApp/1.0 Chrome/138.0.7204.251 Electron/37.10.3 Safari/537.36"},
            [{"type": "page", "title": "Settings", "url": "file:///tmp/settings.html"}],
        )
        self.assertFalse(gb.available())
        self.assertEqual("cdp_port_foreign", gb.block[0])
        self.assertIn("other Electron app", gb.block[1])

    def test_workbuddy_port_is_accepted_and_cached(self):
        gb = GrowthBrowser(_cfg())
        calls: list[str] = []

        def fake(path: str):
            calls.append(path)
            if path.startswith("/json/version"):
                return {"Browser": "Chrome/138.0.7204.251",
                        "User-Agent": "Mozilla/5.0 WorkBuddy/5.11.2 Chrome/138 Electron/37.10.3"}
            return [{"type": "page", "title": "WorkBuddy",
                     "url": "file:///Applications/WorkBuddy.app/Contents/Resources/app.asar/renderer.html"}]

        gb._http = fake
        self.assertTrue(gb.available())
        self.assertIsNone(gb.block)
        self.assertTrue(gb._owner_ok)
        n = len(calls)
        gb.available()
        self.assertEqual(n + 1, len(calls), "归属判定通过后应缓存，不再重复请求 /json/version")

    def test_empty_target_list_is_not_foreign(self):
        gb = GrowthBrowser(_cfg())
        gb._http = lambda path: ({"User-Agent": "Mozilla/5.0 Chrome/138"}
                                 if path.startswith("/json/version") else [])
        self.assertFalse(gb.available())
        self.assertIsNone(gb.block, "端口活着但暂无 target 不应判成外来端口")


class DialogCloseFallbackTests(unittest.TestCase):
    """5.11.2 内部子页的返回按钮是纯图标（gs-embed-back，textContent 为空）。

    实测 DOM：
      主页面 subpage=0 back=0 close=1   <- close 关闭整个成长中心
      兑换子页 subpage=1 back=1 close=2
    收内部弹层必须点 back；误点 close 会把成长中心整页关掉，同一轮后续的
    派遣就只剩 state=unknown（2026-11-12 线上日志复现）。
    """

    def setUp(self):
        p = patch("buddy_checkin.adapters.growth_browser.time.sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

    def test_icon_back_button_is_used_when_no_text_label(self):
        gb = GrowthBrowser(_cfg())
        gb._click_by_expr = lambda expr: (False, "not_found: ")
        seen: list[str] = []

        def fake_eval(js, timeout=20):
            seen.append(js)
            return True, {"ok": True, "sel": ".gs-embed-back"}

        gb._eval = fake_eval
        self.assertTrue(gb._close_dialog())
        self.assertIn("gs-embed-back", seen[0])

    def test_close_dialog_never_touches_page_close_icon(self):
        from buddy_checkin.adapters import growth_browser as gbmod

        gb = GrowthBrowser(_cfg())
        gb._click_by_expr = lambda expr: (False, "not_found: ")
        probes: list[str] = []

        def fake_eval(js, timeout=20):
            probes.append(js)
            return True, {"ok": False, "reason": "no_selector_hit"}

        gb._eval = fake_eval
        gb._close_dialog()
        self.assertNotIn("gs-embed-close", probes[-1])
        self.assertNotIn(".gs-embed-close", gbmod.DIALOG_BACK_SELECTORS)

    def test_close_reports_false_when_nothing_matches(self):
        gb = GrowthBrowser(_cfg())
        gb._click_by_expr = lambda expr: (False, "not_found: ")
        gb._eval = lambda js, timeout=20: (True, {"ok": False, "reason": "no_selector_hit"})
        self.assertFalse(gb._close_dialog())

    def test_text_label_wins_without_selector_probe(self):
        gb = GrowthBrowser(_cfg())
        gb._click_by_expr = lambda expr: (True, "关闭")
        gb._eval = lambda js, timeout=20: self.fail("文案命中时不该再探图标选择器")
        self.assertTrue(gb._close_dialog())

    def test_close_page_uses_page_close_icon(self):
        from buddy_checkin.adapters import growth_browser as gbmod

        gb = GrowthBrowser(_cfg())
        present = {"v": True}
        gb._iframe_ws = lambda: "ws://x" if present["v"] else None
        gb._close_dialog = lambda: True
        used: list[tuple] = []

        def fake_selectors(sels):
            used.append(sels)
            present["v"] = False
            return True, ".gs-embed-close"

        gb._click_selectors = fake_selectors
        self.assertTrue(gb.close_page())
        self.assertEqual(gbmod.PAGE_CLOSE_SELECTORS, used[0])
        self.assertFalse(present["v"])


class AgentBrowserEnvTests(unittest.TestCase):
    """launchd 下 PATH 只有 /usr/bin:/bin:/usr/sbin:/sbin，而 agent-browser 的
    shebang 是 #!/usr/bin/env node —— 不补 PATH 就每轮 connect failed。"""

    def test_path_prepends_node_dir(self):
        gb = GrowthBrowser(_cfg())
        if not gb.node_bin:
            self.skipTest("本机没有 node")
        with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False):
            env = gb._ab_env()
        parts = env["PATH"].split(":")
        self.assertEqual(str(Path(gb.node_bin).parent), parts[0])
        self.assertTrue(env["PATH"].endswith("/usr/bin:/bin"))

    def test_ab_subprocess_receives_patched_env(self):
        gb = GrowthBrowser(_cfg())
        seen = {}

        def fake_run(cmd, **kw):
            seen.update(kw)

            class R:
                returncode = 0
                stdout = "ok"
                stderr = ""

            return R()

        with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False):
            with patch("buddy_checkin.adapters.growth_browser.subprocess.run", fake_run):
                code, out = gb._ab("tab")
        self.assertEqual(0, code)
        self.assertIn("PATH", seen.get("env", {}))
        self.assertNotEqual("/usr/bin:/bin", seen["env"]["PATH"])


if __name__ == "__main__":
    unittest.main()
