"""跨零点签到假阳性守卫（2026-11-13 实际漏签）。

事故：11-13 00:00:51 读活跃地图，`is-today` 还停在前一天（streak 仍是 11-12 的
10d），程序判定「今天已打卡」并写入 last_checkin_day=2026-11-13，当天不再检查，
结果 11-13 真漏签（11-14 读到的 missed 里就有 13）。

修法：把北京时间今天的日号注入判定脚本，比对 is-today 格子上的日号；
对不上就认为日历未切日，不落库、留给下一轮重试。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from buddy_checkin.adapters.base import ActionResult  # noqa: E402
from buddy_checkin.adapters.growth_browser import GrowthBrowser  # noqa: E402
from buddy_checkin.timeutil import day_key  # noqa: E402


def _cfg() -> dict:
    return {"growth": {"enabled": True, "cdp_port": 9222}}


class RolloverGuardTests(unittest.TestCase):
    def setUp(self):
        p = patch("buddy_checkin.adapters.growth_browser.time.sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)
        self.gb = GrowthBrowser(_cfg())
        self.gb.ensure_page = lambda: ActionResult(True, "growth_open", "open", {})
        self.gb._close_dialog = lambda: False

    def test_js_uses_today_day_number(self):
        day = int(day_key().rsplit("-", 1)[-1])
        js = self.gb._checkin_js()
        self.assertNotIn("%d", js, "占位符必须被替换掉")
        self.assertIn(f"const expected = {day};", js)
        self.assertIn("previousElementSibling", js, "切日判定要用昨日格子比对")

    def test_unswitched_calendar_blocks_confirm(self):
        """读到前一天的数据时绝不能判成功。"""
        self.gb.checkin_status = lambda: {
            "ok": True, "checked": True, "has_today": True, "switched": False,
            "day_label": "12", "streak": 10, "makeup": [], "bg": "rgb(111,229,198)"}
        r = self.gb.checkin()
        self.assertFalse(r.ok, "日历未切日时不能判签到成功")
        self.assertFalse(r.data.get("confirmed"))
        self.assertIn("rollover", r.detail)

    def test_switched_calendar_keeps_behaviour(self):
        self.gb.checkin_status = lambda: {
            "ok": True, "checked": True, "has_today": True, "switched": True,
            "day_label": "14", "streak": 1, "makeup": ["1", "2", "13"],
            "bg": "rgb(111,229,198)"}
        r = self.gb.checkin()
        self.assertTrue(r.ok)
        self.assertTrue(r.data.get("confirmed"))
        self.assertIn("streak=1d", r.detail)

    def test_unchecked_today_still_fails(self):
        self.gb.checkin_status = lambda: {
            "ok": True, "checked": False, "has_today": True, "switched": True,
            "day_label": "14", "streak": 0, "makeup": []}
        r = self.gb.checkin()
        self.assertFalse(r.ok)
        self.assertIn("NOT recorded", r.detail)


if __name__ == "__main__":
    unittest.main()
