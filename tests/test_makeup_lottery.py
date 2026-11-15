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


class MakeupCalendarGuardTests(unittest.TestCase):
    """2026-11-15：「扫不到可补格」不能等于「没有漏签」。

    日历图例实测：已打卡是同一色系的 4 阶（alpha 0.18~0.82），未打卡是纯白，
    与未来日期 is-todo 同色。所以判定必须叠加 class，且要有渲染完整性校验，
    否则弹窗刚开、格子没渲染完时会被误判成 done 并静默一整天。
    """

    def setUp(self):
        p = patch("buddy_checkin.adapters.growth_browser.time.sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)
        self.gb = _gb()

    @staticmethod
    def _scan(**kw) -> dict:
        base = {
            "ok": True, "items": [], "card_balance": 1,
            "render_ok": True, "today_checked": True,
            "streak_days": 15, "day_of_month": 15,
            "checked_days": [f"2026-11-{d:02d}" for d in range(1, 16)],
        }
        base.update(kw)
        return base

    def test_unrendered_calendar_is_pending_not_done(self):
        self.gb.makeup_scan = lambda: self._scan(
            render_ok=False, render_reason="too_few_cells")
        r = self.gb.makeup()
        self.assertFalse(r.ok)
        self.assertEqual(r.data["state"], "pending")

    def test_today_unchecked_defers_to_checkin(self):
        self.gb.makeup_scan = lambda: self._scan(today_checked=False)
        r = self.gb.makeup()
        self.assertFalse(r.ok)
        self.assertEqual(r.data["state"], "pending")

    def test_gap_without_clickable_is_impossible(self):
        """连登 14 天说明本月缺 1 天，但服务端不给可补入口 → 不可补，不是 done。"""
        days = [f"2026-11-{d:02d}" for d in range(2, 16)]
        self.gb.makeup_scan = lambda: self._scan(checked_days=days, streak_days=14)
        r = self.gb.makeup()
        self.assertTrue(r.ok)
        self.assertEqual(r.data["state"], "impossible")
        self.assertEqual(r.data["gap"], 1)

    def test_checked_days_fewer_than_streak_is_pending(self):
        """已打卡格数少于当前连续登录天数 → 日历数据自相矛盾，不能下结论。"""
        self.gb.makeup_scan = lambda: self._scan(
            checked_days=[f"2026-11-{d:02d}" for d in range(1, 6)])
        r = self.gb.makeup()
        self.assertFalse(r.ok)
        self.assertEqual(r.data["state"], "pending")

    def test_full_month_without_gap_is_done(self):
        self.gb.makeup_scan = lambda: self._scan()
        r = self.gb.makeup()
        self.assertTrue(r.ok)
        self.assertEqual(r.data["state"], "done")


class CalendarScanTests(unittest.TestCase):
    """逐格扫描：缺格必须让 render_ok 为假，否则「无漏签」不可信。"""

    def setUp(self):
        self.gb = _gb()

    @staticmethod
    def _cells(days) -> list[dict]:
        return [{"idx": i, "text": str(d), "cls": "is-checked", "day": d,
                 "date": f"2026-11-{d:02d}", "alpha": 0.36, "rgb": [111, 229, 198],
                 "checked": True, "clickable": False, "future": False,
                 "today": False, "balance": None} for i, d in enumerate(days)]

    def _payload(self, days) -> dict:
        return {
            "ok": True, "month": "2026-11", "today": "2026-11-15",
            "day_of_month": 15, "cells": self._cells(days),
            "today_cell": {"checked": True, "date": "2026-11-15", "today": True},
            "card_balance": 1, "card_text": "补登卡 ×1", "streak_days": 15,
            "todo_count": 8, "render_ok": True, "render_reason": "",
        }

    def test_missing_day_breaks_render_ok(self):
        days = [d for d in range(1, 16) if d != 7]
        gb = self.gb
        gb._eval = lambda js, timeout=20: (True, self._payload(days))
        scan = gb.calendar_scan()
        self.assertFalse(scan["render_ok"])
        self.assertIn("missing_days", scan["render_reason"])

    def test_complete_calendar_reports_no_gap(self):
        gb = self.gb
        gb._eval = lambda js, timeout=20: (True, self._payload(range(1, 16)))
        scan = gb.calendar_scan()
        self.assertTrue(scan["render_ok"])
        self.assertEqual(len(scan["checked_days"]), 15)
        self.assertEqual(scan["past_unchecked"], [])
        self.assertTrue(scan["today_checked"])

    def test_unchecked_past_day_becomes_makeup_candidate(self):
        days = [d for d in range(1, 16) if d != 3]
        payload = self._payload(days)
        payload["cells"].append({
            "idx": 99, "text": "3", "cls": "is-broken is-makeup-clickable",
            "day": 3, "date": "2026-11-03", "alpha": 1.0, "rgb": [250, 250, 250],
            "checked": False, "clickable": True, "future": False,
            "today": False, "balance": 1})
        gb = self.gb
        gb._eval = lambda js, timeout=20: (True, payload)
        scan = gb.calendar_scan()
        self.assertTrue(scan["render_ok"])
        self.assertEqual([c["date"] for c in scan["clickable"]], ["2026-11-03"])
        self.assertEqual([i["date"] for i in scan["items"]], ["2026-11-03"])


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
