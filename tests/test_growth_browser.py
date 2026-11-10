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


def _seq_runner(list_windows, states):
    """按 get_window_state 调用次序返回不同快照，模拟点击后的页面变化。"""
    calls = {"i": 0}

    def _run(cmd, **_kw):
        tool = cmd[2] if len(cmd) > 2 else ""
        if tool == "list_windows":
            return _FakeProc(json.dumps(list_windows, ensure_ascii=False))
        if tool == "get_window_state":
            i = calls["i"]
            calls["i"] += 1
            snap = states[min(i, len(states) - 1)]
            return _FakeProc(json.dumps(snap, ensure_ascii=False))
        return _FakeProc("{}")  # click 等

    return _run


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

    def test_claim_confirmed_without_已领取_text(self):
        """回归（2026-11-10 真实踩坑）：点完「领取 10 积分」后页面直接回到可派遣，
        AX 树没有「已领取」字样，不能误报 FAIL（礼物其实已到账）。"""
        wins = _list_windows(
            {"app_name": "Microsoft Edge", "title": "WorkBuddy - 个人中心",
             "pid": 446, "window_id": 60, "bounds": {"width": 1900, "height": 940},
             "is_on_screen": True},
        )
        # _window 探窗(1) -> claim_gift 初读(2) -> 点领取礼物后的弹窗(3) -> 点积分后(4)
        states = [
            _state(["领取礼物", "兑换奖励"]),            # 探窗：判定为成长中心
            _state(["领取礼物"]),                        # 初读：claimable
            _state(["领取 10 积分"]),                    # 弹窗：gift_dialog
            _state(["派猫猫旅行"]),                      # 领完：直接 idle，无「已领取」
        ]
        g = GrowthCenter({"growth": {"browser_app": "auto"}}, runner=_seq_runner(wins, states))
        res = g.claim_gift()
        self.assertTrue(res.ok, res.detail)
        self.assertIn("领取 10 积分", res.detail)

    def test_claim_still_claimable_reports_failure(self):
        """页面仍停在可领状态（没领成功）时必须如实报 FAIL。"""
        wins = _list_windows(
            {"app_name": "Microsoft Edge", "title": "WorkBuddy - 个人中心",
             "pid": 446, "window_id": 60, "bounds": {"width": 1900, "height": 940},
             "is_on_screen": True},
        )
        states = [
            _state(["领取礼物"]),
            _state(["领取礼物"]),
            _state(["领取 10 积分"]),
            _state(["领取礼物"]),  # 点击后仍可领
        ]
        g = GrowthCenter({"growth": {"browser_app": "auto"}}, runner=_seq_runner(wins, states))
        self.assertFalse(g.claim_gift().ok)


class DialogTests(unittest.TestCase):
    """动作完成后的浮层清理 + 领奖成功判定（用户 2026-11-10 口径）。"""

    def _run_dismiss(self, labels):
        clicks = []
        snap = {"elements": [
            {"role": "AXButton", "label": lab, "element_index": idx}
            for idx, lab in enumerate(labels, start=200)
        ], "raw": ""}

        def runner(cmd, **_kw):
            tool = cmd[2] if len(cmd) > 2 else ""
            if tool == "get_window_state":
                return _FakeProc(json.dumps(snap, ensure_ascii=False))
            if tool == "click":
                clicks.append(json.loads(cmd[3]))
            return _FakeProc("{}")

        g = GrowthCenter({"growth": {}}, runner=runner)
        return g.dismiss_dialog(446, 60), clicks

    def test_closes_overlay_and_never_clicks_dispatch_button(self):
        ok, clicks = self._run_dismiss(["已领取", "关闭", "确定派出"])
        self.assertTrue(ok)
        self.assertEqual(len(clicks), 1)
        self.assertEqual(clicks[0]["element_index"], 201)  # 「关闭」，不是「确定派出」

    def test_no_dialog_hint_does_nothing(self):
        ok, clicks = self._run_dismiss(["派猫猫旅行", "兑换奖励"])
        self.assertFalse(ok)
        self.assertEqual(clicks, [])

    def test_toolbar_close_button_is_not_clicked(self):
        # 浏览器工具栏的「关闭」元素索引很小，不能被误当成页面弹窗
        clicks = []
        snap = {"elements": [
            {"role": "AXButton", "label": "已领取", "element_index": 5},
            {"role": "AXButton", "label": "关闭", "element_index": 8},
        ], "raw": ""}

        def runner(cmd, **_kw):
            if cmd[2] == "get_window_state":
                return _FakeProc(json.dumps(snap, ensure_ascii=False))
            if cmd[2] == "click":
                clicks.append(cmd[3])
            return _FakeProc("{}")

        g = GrowthCenter({"growth": {}}, runner=runner)
        self.assertFalse(g.dismiss_dialog(446, 60))
        self.assertEqual(clicks, [])

    def test_claim_success_when_page_returns_to_dispatchable(self):
        self.assertTrue(GrowthCenter._claim_confirmed(_state(["派猫猫旅行"])))
        self.assertTrue(GrowthCenter._claim_confirmed(_state(["旅行倒计时 01:00:00"])))

    def test_claim_not_confirmed_while_still_claimable_or_unknown(self):
        self.assertFalse(GrowthCenter._claim_confirmed(_state(["领取礼物"])))
        self.assertFalse(GrowthCenter._claim_confirmed(_state(["某个陌生页面"])))

    def test_tick_clears_leftover_dialog_then_reads_real_countdown(self):
        """回归（2026-11-10 11:19 真实踩坑）：上一轮领奖后的残留弹窗遮挡本轮，
        tick 必须先清浮层再读状态，否则整轮误判 gift_dialog 失败。"""
        wins = _list_windows(
            {"app_name": "Microsoft Edge", "title": "WorkBuddy - 个人中心",
             "pid": 446, "window_id": 60, "bounds": {"width": 1900, "height": 940},
             "is_on_screen": True},
        )
        overlay = {"elements": [
            {"role": "AXButton", "label": "已领取", "element_index": 200},
            {"role": "AXButton", "label": "关闭", "element_index": 201},
        ], "raw": ""}
        states = [
            _state(["派猫猫旅行", "兑换奖励"]),  # 探窗：成长中心
            overlay,                            # 残留的领奖确认浮层
            _state(["旅行倒计时 01:30:58"]),     # 清理后的真实状态
        ]
        g = GrowthCenter({"growth": {"browser_app": "auto"}},
                         runner=_seq_runner(wins, states))
        res = g.tick(force=False)
        self.assertTrue(res.ok, res.detail)
        self.assertIn("real countdown", res.detail)
        self.assertEqual(res.data.get("remaining_sec"), 5458)


if __name__ == "__main__":
    unittest.main()
