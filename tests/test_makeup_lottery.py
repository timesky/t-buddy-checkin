"""补签 / 抽奖 / 派遣任务状态：DOM 事实与账本状态的边界。

背景（2026-11-14 实测）：
  - 漏签格是 `.gs-cal-cell.is-broken.is-makeup-clickable`，补签提示是
    `.gs-cell-toast--makeup` + 「使用」按钮，且**只认真实鼠标事件**
    （JS 的 element.click() 点它毫无反应）。
  - 抽奖按钮「抽奖(N)」打开的是**子页**（福利抽奖 / 每日任务轮盘），
    转盘按钮 `button.gs-wheel-center`，不是弹层。
  - 派遣任务里适配器 data["state"] 是页面状态（idle/traveling），与账本任务
    状态（done/pending/impossible）同名不同义，必须显式区分。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from buddy_checkin.adapters.base import ActionResult  # noqa: E402
from buddy_checkin.adapters.growth_browser import GrowthBrowser  # noqa: E402


def _cfg() -> dict:
    return {"growth": {"cdp_port": 9222}, "travel": {"enabled": True}}


def _gb() -> GrowthBrowser:
    gb = GrowthBrowser(_cfg())
    gb.ensure_page = lambda: ActionResult(True, "growth_open", "open", {})
    gb._close_dialog = lambda: True
    gb.close_page = lambda attempts=2: True
    gb._opened_this_run = False
    return gb


class MakeupTests(unittest.TestCase):
    def setUp(self):
        p = patch("buddy_checkin.adapters.growth_browser.time.sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)
        self.gb = _gb()

    def test_no_missed_day_is_done(self):
        self.gb.makeup_scan = lambda: {"ok": True, "items": [], "card_balance": 1}
        r = self.gb.makeup()
        self.assertTrue(r.ok)
        self.assertEqual(r.data["state"], "done")

    def test_no_card_left_is_impossible_not_pending(self):
        self.gb.makeup_scan = lambda: {
            "ok": True, "items": [{"date": "2026-11-02"}], "card_balance": 0}
        r = self.gb.makeup()
        self.assertEqual(r.data["state"], "impossible")
        self.assertTrue(r.ok)

    def test_scan_failure_is_pending(self):
        self.gb.makeup_scan = lambda: {"ok": False, "detail": "cdp down", "items": []}
        r = self.gb.makeup()
        self.assertFalse(r.ok)
        self.assertEqual(r.data["state"], "pending")

    def test_making_up_all_days_reports_done(self):
        scans = [
            {"ok": True, "items": [{"date": "2026-11-01"}, {"date": "2026-11-02"}],
             "card_balance": 2},
            {"ok": True, "items": [], "card_balance": 0},
        ]
        self.gb.makeup_scan = lambda: scans.pop(0)
        self.gb._makeup_one = lambda date: (True, f"{date} 补签成功")
        r = self.gb.makeup()
        self.assertTrue(r.ok)
        self.assertEqual(r.data["state"], "done")
        self.assertEqual(r.data["made_up"], ["2026-11-01", "2026-11-02"])

    def test_failed_makeup_stays_pending(self):
        self.gb.makeup_scan = lambda: {
            "ok": True, "items": [{"date": "2026-11-02"}], "card_balance": 1}
        self.gb._makeup_one = lambda date: (False, f"{date}: makeup tip not shown")
        r = self.gb.makeup()
        self.assertFalse(r.ok)
        self.assertEqual(r.data["state"], "pending")

    def test_makeup_one_rejects_off_screen_cell(self):
        """格子没滚进视口时点不中：必须报错而不是当作已补。"""
        self.gb._makeup_cell_rect = lambda d: (True, {"ok": True, "cx": 10, "cy": 900,
                                                     "vh": 640})
        ok, detail = self.gb._makeup_one("2026-11-02")
        self.assertFalse(ok)
        self.assertIn("off-screen", detail)

    def test_makeup_uses_trusted_input_not_js_click(self):
        """补签必须走 CDP Input.dispatchMouseEvent（JS click 无效）。"""
        calls: list[tuple[int, int]] = []
        self.gb._makeup_cell_rect = lambda d: (
            True, {"ok": True, "cx": 100, "cy": 300, "vh": 640})
        self.gb._makeup_tip_rect = lambda: (
            True, {"ok": True, "cx": 200, "cy": 320, "text": "消耗一张补登卡 使用",
                   "btn": "使用"})
        self.gb._input_click = lambda x, y, timeout=15: (calls.append((x, y)) or (True, "ok"))
        scans = [{"ok": True, "items": [], "card_balance": 0}]
        self.gb.makeup_scan = lambda: scans[0]
        ok, detail = self.gb._makeup_one("2026-11-02")
        self.assertTrue(ok, detail)
        self.assertEqual(calls, [(100, 300), (200, 320)])


class LotteryTests(unittest.TestCase):
    def setUp(self):
        p = patch("buddy_checkin.adapters.growth_browser.time.sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)
        self.gb = _gb()

    def test_no_ticket_is_done(self):
        self.gb.lottery_tickets = lambda st=None: (0, "抽奖(0)")
        r = self.gb.lottery()
        self.assertTrue(r.ok)
        self.assertEqual(r.data["state"], "done")

    def test_draws_until_no_ticket_left(self):
        reads = [(2, "抽奖(2)"), (0, "抽奖(0)"), (0, "抽奖(0)")]
        self.gb.lottery_tickets = lambda st=None: reads.pop(0) if reads else (0, "抽奖(0)")
        self.gb._click_text = lambda t: (True, t)
        self.gb._click_selectors = lambda sels: (True, ".gs-wheel-center")
        self.gb._wait_draw_result = lambda timeout=12.0: 0
        r = self.gb.lottery()
        self.assertTrue(r.ok)
        self.assertEqual(r.data["state"], "done")
        self.assertEqual(r.data["draws"], 1)
        self.assertEqual(r.data["tickets_left"], 0)

    def test_wheel_missing_is_pending(self):
        self.gb.lottery_tickets = lambda st=None: (1, "抽奖(1)")
        self.gb._click_text = lambda t: (True, t)
        self.gb._click_selectors = lambda sels: (False, "no_selector_hit")
        r = self.gb.lottery()
        self.assertFalse(r.ok)
        self.assertEqual(r.data["state"], "pending")

    def test_left_tickets_means_pending(self):
        self.gb.lottery_tickets = lambda st=None: (2, "抽奖(2)")
        self.gb._click_text = lambda t: (True, t)
        self.gb._click_selectors = lambda sels: (True, ".gs-wheel-center")
        self.gb._wait_draw_result = lambda timeout=12.0: 1
        r = self.gb.lottery(max_draws=1)
        self.assertFalse(r.ok)
        self.assertEqual(r.data["state"], "pending")
        self.assertEqual(r.data["tickets_left"], 2)


class TravelStateTests(unittest.TestCase):
    def test_page_state_does_not_leak_into_task_state(self):
        """适配器的页面状态（traveling）不能当成账本任务状态。"""
        from buddy_checkin.actions import Actions
        from buddy_checkin.progress import PENDING

        class G:
            def tick(self, force=False):
                return ActionResult(True, "travel", "traveling; 4863s",
                                    {"state": "traveling", "due_at": 12345.0})

        class S:
            def kv_set(self, *a):
                pass

            def log(self, *a):
                pass

        actions = Actions(_cfg(), S(), None, growth=G())
        r = actions.travel_tick()
        self.assertEqual(r.data["state"], PENDING)
        self.assertEqual(r.data["page_state"], "traveling")

    def test_dispatch_exhausted_finishes_travel_task(self):
        from buddy_checkin.actions import Actions
        from buddy_checkin.progress import DONE

        class G:
            def tick(self, force=False):
                return ActionResult(False, "travel", "dispatch exhausted today",
                                    {"state": "idle", "dispatch_blocked": "exhausted"})

        class S:
            def kv_set(self, *a):
                pass

            def log(self, *a):
                pass

        actions = Actions(_cfg(), S(), None, growth=G())
        r = actions.travel_tick()
        self.assertEqual(r.data["state"], DONE)


if __name__ == "__main__":
    unittest.main()
