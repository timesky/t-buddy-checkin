"""成长中心浏览器无关探测 + 未登录提示的单测（不启动 GUI）。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from buddy_checkin.adapters.growth import GrowthCenter  # noqa: E402


class _FakeProc:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def _runner(responses: dict):
    def _run(cmd, **_kw):
        tool = cmd[2] if len(cmd) > 2 else ""
        return _FakeProc(json.dumps(responses.get(tool, {}), ensure_ascii=False))

    return _run


def _list_windows(*wins):
    return {"windows": list(wins)}


def _state(labels, raw=""):
    return {"elements": [{"role": "AXButton", "label": l, "element_index": i}
                         for i, l in enumerate(labels)], "raw": raw}


class GrowthBrowserTests(unittest.TestCase):
    def test_auto_detects_chrome_growth_window_not_edge(self):
        cfg = {"growth": {"browser_app": "auto", "title_hint": "个人中心"}}
        responses = {
            "list_windows": _list_windows(
                {"app_name": "Google Chrome", "title": "个人中心 - WorkBuddy",
                 "pid": 1001, "window_id": 10, "bounds": {"width": 1200, "height": 800},
                 "is_on_screen": True},
                {"app_name": "Microsoft Edge", "title": "WorkBuddy",
                 "pid": 2002, "window_id": 20, "bounds": {"width": 1000, "height": 700},
                 "is_on_screen": True},
            ),
            "get_window_state": _state(["派猫猫旅行", "兑换奖励"]),
        }
        g = GrowthCenter(cfg, runner=_runner(responses))
        win = g._window()
        self.assertIsNotNone(win)
        self.assertEqual(win, (1001, 10))  # 命中 Chrome 的成长中心，而非 Edge
        self.assertIsNone(g._block)

    def test_login_page_sets_login_required_block(self):
        cfg = {"growth": {"browser_app": "auto", "title_hint": "个人中心"}}
        responses = {
            "list_windows": _list_windows(
                {"app_name": "Safari", "title": "WorkBuddy",
                 "pid": 3003, "window_id": 30, "bounds": {"width": 1100, "height": 750},
                 "is_on_screen": True},
            ),
            "get_window_state": _state(["微信扫码登录", "其他登录方式"]),
        }
        g = GrowthCenter(cfg, runner=_runner(responses))
        win = g._window()
        self.assertIsNone(win)
        self.assertEqual(g._block[0], "login_required")
        res = g._block_result("travel")
        self.assertFalse(res.ok)
        self.assertEqual(res.data.get("block"), "login_required")

    def test_logged_in_page_with_incidental_login_text_is_not_misjudged(self):
        """回归（2026-11-09 真实踩坑）：已登录成长中心页面夹杂「…请登录」文案，
        不能误判为未登录——强成长内容优先于登录标记判定。"""
        cfg = {"growth": {"browser_app": "auto", "title_hint": "个人中心"}}
        responses = {
            "list_windows": _list_windows(
                {"app_name": "Microsoft Edge", "title": "WorkBuddy - 个人中心",
                 "pid": 446, "window_id": 60, "bounds": {"width": 1900, "height": 940},
                 "is_on_screen": True},
            ),
            # 真实 AX 树：领取礼物在场（已登录、可领奖），但侧栏有「请登录」文案
            "get_window_state": _state([
                "WorkBuddy - 个人中心 - Microsoft Edge",
                "领取礼物", "兑换奖励", "用户配置 1 个人资料，请登录",
            ]),
        }
        g = GrowthCenter(cfg, runner=_runner(responses))
        win = g._window()
        self.assertIsNotNone(win)
        self.assertEqual(win, (446, 60))
        self.assertIsNone(g._block)

    def test_explicit_browser_app_restricts_to_that_app(self):
        cfg = {"growth": {"browser_app": "Microsoft Edge", "title_hint": "个人中心"}}
        responses = {
            "list_windows": _list_windows(
                {"app_name": "Google Chrome", "title": "个人中心 - WorkBuddy",
                 "pid": 1001, "window_id": 10, "bounds": {"width": 1200, "height": 800},
                 "is_on_screen": True},
                {"app_name": "Microsoft Edge", "title": "个人中心",
                 "pid": 2002, "window_id": 20, "bounds": {"width": 1000, "height": 700},
                 "is_on_screen": True},
            ),
            "get_window_state": _state(["领取礼物"]),
        }
        g = GrowthCenter(cfg, runner=_runner(responses))
        win = g._window()
        self.assertEqual(win, (2002, 20))  # 显式指定 Edge，只认 Edge

    def test_no_window_sets_not_found_block(self):
        cfg = {"growth": {"browser_app": "auto", "title_hint": "个人中心"}}
        responses = {"list_windows": _list_windows()}
        g = GrowthCenter(cfg, runner=_runner(responses))
        self.assertIsNone(g._window())
        self.assertEqual(g._block[0], "not_found")


if __name__ == "__main__":
    unittest.main()
