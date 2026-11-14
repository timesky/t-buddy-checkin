"""浮层签到通道（FuelCard）单测。

背景（2026-11-14）：签到从来不在成长中心，而主窗口左下角「Buddy加油站」浮层
里。CDP 重构后把签到判据换成「读成长中心活跃地图」是错的 —— 活跃地图只代表
「今天用过客户端」，于是签到其实一次都没做却一直报 confirmed=True。
这些用例锁住三件事：
  1. 只有真的看到浮层签到卡才认为「可领」；
  2. 只有真的领到（toast/卡片消失）才认为「已签」；
  3. 绝不点关闭按钮，也绝不把「浮层不在」当成「已签」。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from buddy_checkin.adapters.fuel_card import FuelCard  # noqa: E402


def _cfg() -> dict:
    return {"growth": {"cdp_port": 9222}}


def _card(**kw) -> dict:
    base = {
        "host_cls": "wb-slot wb-slot--avatar-top-combo",
        "title": "Buddy加油站",
        "sub": "每日可领 100 积分",
        "toast": "",
        "btn": "立即领取",
        "btn_disabled": False,
        "props": {"index": 0, "type": "checkin"},
        "dots": [{"on": True, "label": "Buddy加油站"},
                 {"on": False, "label": "活动通知"}],
        "visible": True,
    }
    base.update(kw)
    return base


class FuelProbeTests(unittest.TestCase):
    def setUp(self):
        self.fc = FuelCard(_cfg())

    def _probe_with(self, cards):
        self.fc._eval = lambda js, timeout=20: (True, {"cards": cards})
        return self.fc.probe()

    def test_checkin_card_present_means_claimable(self):
        st = self._probe_with([_card()])
        self.assertTrue(st["ok"])
        self.assertTrue(st["present"])
        self.assertEqual(st["card"]["btn"], "立即领取")

    def test_no_checkin_item_means_already_claimed(self):
        st = self._probe_with([_card(
            title="Hy4 正式版发布", sub="限时免费", btn="立即使用",
            props={"index": 0, "type": "notice"},
            dots=[{"on": True, "label": "活动通知"}])])
        self.assertFalse(st["present"])
        self.assertTrue(st["claimed"])

    def test_missing_bubble_is_unknown_not_claimed(self):
        """浮层被关掉/未登录时必须是未知，绝不能当已签（否则整天不再检查）。"""
        st = self._probe_with([])
        self.assertTrue(st["ok"])
        self.assertFalse(st["present"])
        self.assertIsNone(st["claimed"])

    def test_probe_error_is_not_claimed(self):
        self.fc._eval = lambda js, timeout=20: (False, "cdp eval timeout")
        st = self.fc.probe()
        self.assertFalse(st["ok"])
        self.assertIsNone(st["claimed"])

    def test_switches_to_checkin_card_when_other_card_active(self):
        """轮播当前停在通知卡时，要把签到卡切出来再读，不能直接判「已领」。"""
        notice = _card(title="活动通知", btn="立即查看",
                       props={"index": 0, "type": "notice"},
                       dots=[{"on": True, "label": "活动通知"},
                             {"on": False, "label": "Buddy加油站"}])
        checkin = _card()
        calls = {"n": 0}

        def fake_eval(js, timeout=20):
            if "dots[" in js:          # 切卡
                return True, {"ok": True}
            calls["n"] += 1
            return True, {"cards": [notice if calls["n"] == 1 else checkin]}

        self.fc._eval = fake_eval
        st = self.fc.probe(switch_to_checkin=True)
        self.assertTrue(st["present"])
        self.assertEqual(st["card"]["title"], "Buddy加油站")


class FuelClaimTests(unittest.TestCase):
    def setUp(self):
        self.fc = FuelCard(_cfg())
        p = patch("buddy_checkin.adapters.fuel_card.time.sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)

    def test_already_claimed_does_not_click(self):
        self.fc.probe = lambda switch_to_checkin=False: {
            "ok": True, "present": False, "claimed": True, "card": {}, "cards": []}
        self.fc._eval = lambda *a, **k: self.fail("已领过不该再点按钮")
        r = self.fc.claim()
        self.assertTrue(r.ok)
        self.assertTrue(r.data.get("already"))

    def test_unknown_bubble_does_not_claim(self):
        self.fc.probe = lambda switch_to_checkin=False: {
            "ok": True, "present": False, "claimed": None,
            "detail": "fuel card not rendered", "card": {}, "cards": []}
        self.fc._eval = lambda *a, **k: self.fail("浮层不在时不该盲点")
        r = self.fc.claim()
        self.assertFalse(r.ok)
        self.assertFalse(r.data.get("confirmed"))

    def test_success_toast_confirms(self):
        seq = [
            {"ok": True, "present": True, "claimed": False, "card": _card(), "cards": []},
            {"ok": True, "present": False, "claimed": True,
             "card": _card(toast="领取成功", btn="已领取"), "cards": []},
        ]
        self.fc.probe = lambda switch_to_checkin=False: seq.pop(0) if seq else {
            "ok": True, "present": False, "claimed": True,
            "card": _card(toast="领取成功"), "cards": []}
        self.fc._eval = lambda js, timeout=20: (True, {"ok": True, "btn": "立即领取"})
        r = self.fc.claim()
        self.assertTrue(r.ok)
        self.assertTrue(r.data.get("confirmed"))

    def test_click_without_confirmation_is_failure(self):
        self.fc.probe = lambda switch_to_checkin=False: {
            "ok": True, "present": True, "claimed": False, "card": _card(), "cards": []}
        self.fc._eval = lambda js, timeout=20: (True, {"ok": True, "btn": "立即领取"})
        r = self.fc.claim(wait_sec=0.1)
        self.assertFalse(r.ok, "点了但没确认，不能报成功")

    def test_click_js_only_targets_checkin_button(self):
        """只许点 #go 且 props.type==='checkin'；关闭按钮 #x 永远不碰。"""
        js = FuelCard._click_js()
        self.assertIn("getElementById('go')", js)
        self.assertIn("'checkin'", js)
        self.assertNotIn("#x", js)
        self.assertNotIn("'x'", js)
        self.assertNotIn("dot", js)


if __name__ == "__main__":
    unittest.main()
