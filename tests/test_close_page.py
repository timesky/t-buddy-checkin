"""成长中心弹窗收尾行为（2026-11-12 用户要求：任务完成后不要留弹窗）。

三条硬约束：
  1. 本次亲手打开的弹窗 → 任务结束后必须关；
  2. 用户自己开着的弹窗 → 一个字都不许动（只收内部弹层）；
  3. 嵌套调用（tick → claim_gift/dispatch）只在最外层收一次。
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
    g = {"enabled": True, "cdp_port": 9222}
    g.update(growth)
    return {"growth": g}


class DialogCloseTests(unittest.TestCase):
    def _browser(self, iframe_present: bool, cfg: dict | None = None) -> GrowthBrowser:
        gb = GrowthBrowser(cfg or _cfg())
        self._present = {"v": iframe_present}
        self.gb = gb
        gb._iframe_ws = lambda: "ws://x" if self._present["v"] else None
        gb._close_dialog = lambda: False
        gb.close_page = lambda attempts=2: self._mark_closed()
        return gb

    def _mark_closed(self) -> bool:
        self._present["v"] = False
        self.gb.last_close_ok = True
        return True

    def test_page_opened_by_tool_is_closed_after_checkin(self):
        gb = self._browser(iframe_present=False)

        def fake_ensure():
            self._present["v"] = True          # 工具打开了页面
            gb._opened_this_run = True
            return ActionResult(True, "growth_open", "opened", {"opened": True})

        gb.ensure_page = fake_ensure
        gb.checkin_status = lambda: {"ok": True, "checked": True, "streak": 10, "makeup": []}
        r = gb.checkin()
        self.assertTrue(r.ok)
        self.assertFalse(self._present["v"], "工具自己打开的弹窗必须在任务结束后关闭")
        self.assertTrue(gb.last_close_ok)

    def test_user_opened_page_is_left_open(self):
        gb = self._browser(iframe_present=True)
        gb.ensure_page = lambda: ActionResult(
            True, "growth_open", "page already open", {"opened": False})
        gb.checkin_status = lambda: {"ok": True, "checked": True, "streak": 3, "makeup": []}
        r = gb.checkin()
        self.assertTrue(r.ok)
        self.assertTrue(self._present["v"], "用户自己开着的页面不得关闭")
        self.assertFalse(gb.last_close_ok)

    def test_close_can_be_disabled_by_config(self):
        gb = self._browser(iframe_present=False, cfg=_cfg(close_page_after_action=False))

        def fake_ensure():
            self._present["v"] = True
            gb._opened_this_run = True
            return ActionResult(True, "growth_open", "opened", {"opened": True})

        gb.ensure_page = fake_ensure
        gb.checkin_status = lambda: {"ok": True, "checked": True, "streak": 1, "makeup": []}
        gb.checkin()
        self.assertTrue(self._present["v"])

    def test_nested_calls_close_only_once(self):
        """tick 内部调 claim_gift / dispatch 时不得各自收尾（否则反复开关页面）。"""
        gb = self._browser(iframe_present=False)
        closes: list[int] = []

        def fake_ensure():
            self._present["v"] = True
            gb._opened_this_run = True
            return ActionResult(True, "growth_open", "opened", {"opened": True})

        gb.ensure_page = fake_ensure
        gb.close_page = lambda attempts=2: (closes.append(1), self._mark_closed())[1]
        gb._close_dialog = lambda: False
        gb.read_state = lambda: {"state": "traveling", "remaining_sec": 100, "raw": "", "buttons": []}
        r = gb.tick()
        self.assertTrue(r.ok)
        self.assertEqual(1, len(closes), "嵌套调用应只在最外层关闭一次")
        self.assertFalse(self._present["v"])

    def test_wrap_up_survives_exceptions(self):
        """动作内部抛错也必须收尾，不能把弹窗挂在屏幕上。"""
        gb = self._browser(iframe_present=False)

        def boom():
            self._present["v"] = True
            gb._opened_this_run = True
            raise RuntimeError("page broken")

        gb.ensure_page = boom
        with self.assertRaises(RuntimeError):
            gb.checkin()
        self.assertFalse(self._present["v"])


if __name__ == "__main__":
    unittest.main()
