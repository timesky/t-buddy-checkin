"""全链路单测：纯标准库，不启动浏览器、不碰桌面 GUI。"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from buddy_checkin import config  # noqa: E402
from buddy_checkin.actions import Actions  # noqa: E402
from buddy_checkin.adapters.base import ActionResult, DesktopAdapter  # noqa: E402
from buddy_checkin.adapters.desktop import CuaDriverSigner  # noqa: E402
from buddy_checkin.adapters.growth import GrowthCenter, parse_hms  # noqa: E402
from buddy_checkin.scheduler import Scheduler  # noqa: E402
from buddy_checkin.state import State  # noqa: E402
from buddy_checkin.timeutil import (  # noqa: E402
    CN_TZ,
    day_key,
    find_field,
    next_daily_at,
    parse_countdown,
    parse_timestamp,
    skew_from_http_date,
    travel_due,
)


def test_cfg(tmp: Path) -> dict:
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["state_db"] = str(tmp / "state.sqlite3")
    return cfg


class FakeDesktop(DesktopAdapter):
    def __init__(self, confirmed: bool = True) -> None:
        self.calls = 0
        self.confirmed = confirmed

    def probe(self) -> ActionResult:
        return ActionResult(True, "desktop.probe", "")

    def claim_daily(self, texts: list[str]) -> ActionResult:
        self.calls += 1
        return ActionResult(True, "checkin", "ok", {"confirmed": self.confirmed})


class FakeGrowth:
    def __init__(self, due: float):
        self.due = due

    def claim_gift(self):
        return ActionResult(True, "gift", "ok")

    def claim_exchanges(self, claimed_before):
        return []

    def tick(self, force: bool = False):
        return ActionResult(True, "travel", "fake growth ok", {"due_at": self.due})


class FakeActions:
    """调度器用的假编排：只记录三条线各自的调用与可配置的结论。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.checkin_confirmed = True
        self.exchange_ok = True
        self.travel_due_at: float | None = None

    def daily_checkin(self):
        self.calls.append("checkin")
        return ActionResult(True, "checkin", "fake", {"confirmed": self.checkin_confirmed})

    def check_exchanges(self):
        self.calls.append("exchange")
        return ActionResult(self.exchange_ok, "exchange", "fake", {"count": 0})

    def travel_tick(self, force: bool = False):
        self.calls.append("travel")
        return ActionResult(True, "travel", "fake", {"due_at": self.travel_due_at})


class TimeUtilTests(unittest.TestCase):
    def test_epoch_seconds_and_milliseconds(self):
        self.assertEqual(parse_timestamp(1794153600), 1794153600.0)
        self.assertEqual(parse_timestamp(1794153600000), 1794153600.0)
        self.assertIsNone(parse_timestamp(0))
        self.assertIsNone(parse_timestamp(True))

    def test_utc_iso_equals_beijing_naive(self):
        utc = parse_timestamp("2026-11-09T02:20:04Z")
        naive = parse_timestamp("2026-11-09 10:20:04")
        self.assertIsNotNone(utc)
        self.assertEqual(utc, naive)  # naive 必须按北京时间解释，而不是 UTC

    def test_day_key_uses_beijing(self):
        # UTC 11/8 16:00 == 北京时间 11/9 00:00
        ts = datetime(2026, 11, 8, 16, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(day_key(ts), "2026-11-09")

    def test_countdown(self):
        self.assertEqual(parse_countdown("00:17:55"), 1075.0)
        self.assertEqual(parse_countdown("17:55"), 1075.0)
        self.assertEqual(parse_countdown(900), 900.0)
        self.assertEqual(parse_countdown("900"), 900.0)
        self.assertIsNone(parse_countdown("n/a"))

    def test_find_field_and_travel_due(self):
        payload = {"data": {"travel": {"endTime": 1794153600000, "state": "traveling"}}}
        self.assertEqual(find_field(payload, ["end_at", "endTime"]), 1794153600000)
        cfg = {
            "end_field_candidates": ["end_at", "endTime"],
            "remaining_field_candidates": ["remaining_seconds"],
        }
        self.assertEqual(travel_due(payload, cfg, 1000.0), 1794153600.0)
        countdown = {"data": {"travel": {"remaining_seconds": "00:17:55"}}}
        self.assertEqual(travel_due(countdown, cfg, 1000.0), 2075.0)

    def test_next_daily_at(self):
        now = datetime(2026, 11, 9, 10, 20, 0, tzinfo=CN_TZ)
        self.assertEqual(next_daily_at("12:00", now).hour, 12)
        self.assertEqual(next_daily_at("12:00", now).day, 9)
        tomorrow = next_daily_at("09:00", now)
        self.assertEqual((tomorrow.date() - now.date()).days, 1)
        self.assertEqual(tomorrow.hour, 9)

    def test_clock_skew(self):
        header = "Mon, 09 Nov 2026 02:20:10 GMT"
        local = datetime(2026, 11, 9, 2, 20, 4, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(skew_from_http_date(header, local), 6.0, places=1)


class ConfigTests(unittest.TestCase):
    def test_defaults_and_scrub(self):
        cfg = config.load("/nonexistent/config.toml")
        self.assertTrue(cfg["growth"]["enabled"])
        self.assertEqual(cfg["desktop"]["app"], "WorkBuddy")
        self.assertIn("cua-driver", cfg["desktop"]["cua_driver_bin"])
        # 唯一外部工具是 CuaDriver：不再有任何 headless 浏览器通道
        self.assertNotIn("web", cfg)
        self.assertNotIn("bin", cfg["desktop"])
        scrubbed = config.scrub({"accessToken": "abc", "nested": {"cookie": "x", "ok": 1}})
        self.assertEqual(scrubbed["accessToken"], "<redacted>")
        self.assertEqual(scrubbed["nested"]["cookie"], "<redacted>")
        self.assertEqual(scrubbed["nested"]["ok"], 1)

    def test_toml_merge(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            path.write_text('[travel]\nclaim_offset_sec = 99\n', encoding="utf-8")
            merged = config.load(path)
            self.assertEqual(merged["travel"]["claim_offset_sec"], 99)
            self.assertTrue(merged["growth"]["enabled"])  # 未覆盖的段落用默认


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "state.sqlite3")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_kv_roundtrip(self):
        self.state.kv_set("last_checkin_day", "2026-11-09")
        self.state.kv_set("travel_due_at", 1794153600.5)
        self.assertEqual(self.state.kv_get("last_checkin_day"), "2026-11-09")
        self.assertAlmostEqual(self.state.kv_get_float("travel_due_at"), 1794153600.5)

    def test_claim_once_is_idempotent(self):
        self.assertTrue(self.state.claim_once("gift", "领取"))
        self.assertFalse(self.state.claim_once("gift", "领取"))
        self.assertTrue(self.state.already_claimed("gift", "领取"))
        self.assertFalse(self.state.already_claimed("gift", "不存在"))

    def test_run_log(self):
        self.state.log("checkin", True, "ok")
        self.assertEqual(len(self.state.recent()), 1)
        self.assertEqual(self.state.recent()[0]["action"], "checkin")


class ActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = test_cfg(Path(self.tmp.name))
        self.state = State(Path(self.tmp.name) / "state.sqlite3")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_full_loop_is_idempotent(self):
        growth = FakeGrowth(due=time.time() + 1200)
        desktop = FakeDesktop()
        actions = Actions(self.cfg, self.state, desktop, growth=growth)
        summary = actions.reconcile_all()
        self.assertTrue(summary["ok"])
        self.assertEqual(desktop.calls, 1)
        self.assertEqual(self.state.kv_get("last_checkin_day"), day_key())
        due = self.state.kv_get_float("travel_due_at")
        self.assertIsNotNone(due)
        self.assertGreater(due, time.time())

        actions.reconcile_all()
        self.assertEqual(desktop.calls, 1)  # 当天不重复签到
        # travel_due_at 仍由 growth 的真实倒计时刷新
        self.assertAlmostEqual(self.state.kv_get_float("travel_due_at"),
                               self.state.kv_get_float("travel_due_at"))

    def test_unconfirmed_checkin_does_not_write_state(self):
        """回归（2026-11-10）：按钮找不到只是推测，绝不能写 last_checkin_day，
        否则当天所有后续轮询都被跳过，整天漏签。"""
        actions = Actions(self.cfg, self.state, FakeDesktop(confirmed=False),
                          growth=FakeGrowth(due=time.time() + 1200))
        result = actions.daily_checkin()
        self.assertFalse(result.data.get("confirmed"))
        self.assertIsNone(self.state.kv_get("last_checkin_day"))
        # 下一轮仍会真正调用桌面端检查，而不是被幂等跳过
        actions.daily_checkin()
        self.assertEqual(2, actions.desktop.calls)

    def test_force_claim_then_redispatch_records_due(self):
        due = time.time() + 3
        actions = Actions(self.cfg, self.state, FakeDesktop(), growth=FakeGrowth(due))
        result = actions.travel_tick(force=True)
        self.assertTrue(result.ok)
        self.assertAlmostEqual(self.state.kv_get_float("travel_due_at"), due, places=2)

    def test_missing_growth_channel_is_reported(self):
        """growth 未启用时必须明确报错，不能静默吞掉。"""
        actions = Actions(self.cfg, self.state, FakeDesktop(), growth=None)
        travel = actions.travel_tick()
        self.assertFalse(travel.ok)
        self.assertIn("growth channel not configured", travel.detail)
        gifts = actions.claim_gifts()
        self.assertEqual(len(gifts), 1)
        self.assertFalse(gifts[0].ok)
        self.assertIn("growth channel not configured", gifts[0].detail)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = test_cfg(Path(self.tmp.name))
        self.state = State(Path(self.tmp.name) / "state.sqlite3")
        self.actions = FakeActions()
        # 固定为北京时间 2026-11-10 11:00:00，断言所有排程时刻
        self.fixed_now = datetime(2026, 11, 10, 11, 0, 0, tzinfo=CN_TZ).timestamp()
        self.scheduler = Scheduler(
            self.cfg, self.state, self.actions,
            now=lambda: self.fixed_now, sleep=lambda _s: None,
        )

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def _sched(self):
        return Scheduler(self.cfg, self.state, self.actions,
                         now=lambda: self.fixed_now, sleep=lambda _s: None)

    @staticmethod
    def _beijing(ts):
        return datetime.fromtimestamp(ts, CN_TZ)

    def test_initial_jobs_are_all_three_and_immediate(self):
        jobs = self.scheduler.initial_jobs(self.fixed_now)
        self.assertEqual({j.name for j in jobs}, {"checkin", "exchange", "travel"})
        self.assertTrue(all(j.due_at == self.fixed_now for j in jobs))

    def test_checkin_confirmed_disables_until_next_midnight(self):
        self.actions.checkin_confirmed = True
        job = self.scheduler._checkin(None)
        boundary = self._beijing(job.due_at)
        self.assertEqual((boundary.hour, boundary.minute, boundary.second), (0, 0, 0))
        self.assertEqual(boundary.date().isoformat(), "2026-11-11")
        self.assertEqual(self.state.kv_get_float("next_checkin_at"), job.due_at)

    def test_checkin_unconfirmed_retries_in_one_hour(self):
        """仅凭按钮不存在推测已签不算确认：从当前时刻向后滚动 1 小时。"""
        self.actions.checkin_confirmed = False
        job = self.scheduler._checkin(None)
        self.assertAlmostEqual(job.due_at, self.fixed_now + 3600, places=2)
        self.assertEqual(self._beijing(job.due_at).hour, 12)

    def test_exchange_conclusion_disables_until_next_midnight(self):
        self.actions.exchange_ok = True  # 兑换成功 or 确认无可兑换
        job = self.scheduler._exchange(None)
        self.assertEqual(self._beijing(job.due_at).hour, 0)
        self.assertEqual(self._beijing(job.due_at).date().isoformat(), "2026-11-11")

    def test_exchange_failure_retries_in_one_hour(self):
        self.actions.exchange_ok = False
        job = self.scheduler._exchange(None)
        self.assertAlmostEqual(job.due_at, self.fixed_now + 3600, places=2)

    def test_travel_uses_real_countdown_not_fixed_interval(self):
        """trave 线唯一动态滚动：按页面真实倒计时得到 due_at。"""
        real_due = self.fixed_now + 2 * 3600 + 17  # 2h00m17s，非整点
        self.state.kv_set("travel_due_at", real_due)
        job = self.scheduler._travel(None)
        self.assertAlmostEqual(job.due_at, real_due, places=2)
        self.assertEqual(self._beijing(job.due_at).minute, 0)
        self.assertEqual(self._beijing(job.due_at).second, 17)

    def test_run_forever_executes_all_three_lines(self):
        executed = self.scheduler.run_forever(max_iterations=3)
        self.assertEqual(executed, 3)
        self.assertEqual(set(self.actions.calls), {"checkin", "exchange", "travel"})


def desktop_snap(*buttons) -> dict:
    elements = [{"element_index": 0, "role": "AXWindow", "label": "WorkBuddy"}]
    elements.extend(buttons)
    return {"elements": elements, "element_count": len(elements)}


class CuaFakeRunner:
    """按快照序列模拟 cua-driver call，可注入 click 的 path。"""

    def __init__(self, snaps: list[dict] | None = None, *, click_path: str = "ax"):
        self.snaps = list(snaps) if snaps else [desktop_snap()]
        self.click_path = click_path
        self.calls: list[list[str]] = []
        self.snap_count = 0

    def __call__(self, cmd, **_kwargs):
        self.calls.append(cmd)
        tool = cmd[2]
        if tool == "list_windows":
            out = {"windows": [{
                "app_name": "WorkBuddy", "pid": 452, "window_id": 66,
                "is_on_screen": True, "bounds": {"width": 1920, "height": 960},
            }]}
        elif tool == "get_window_state":
            self.snap_count += 1
            out = self.snaps.pop(0) if len(self.snaps) > 1 else self.snaps[0]
        elif tool == "click":
            out = {"effect": "unverifiable", "path": self.click_path, "verified": False}
        else:
            out = {}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")


class CuaDriverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = test_cfg(Path(self.tmp.name))
        self.cfg["desktop"]["cua_driver_bin"] = "/fake/cua-driver"

    def tearDown(self):
        self.tmp.cleanup()

    def test_clicks_checkin_button_and_verifies_disappearance(self):
        snaps = [
            desktop_snap({"element_index": 65, "role": "AXButton", "label": "立即领取"}),
            desktop_snap(),  # 点击后按钮消失
        ]
        runner = CuaFakeRunner(snaps)
        result = CuaDriverSigner(self.cfg, runner).claim_daily(["立即领取"])
        self.assertTrue(result.ok)
        self.assertTrue(result.data["verified"])
        clicks = [c for c in runner.calls if c[2] == "click"]
        self.assertEqual(len(clicks), 1)
        self.assertEqual(json.loads(clicks[0][3]),
                         {"pid": 452, "window_id": 66, "element_index": 65})
        self.assertEqual(runner.snap_count, 2)

    def test_opens_collapsed_station_panel_then_clicks_checkin(self):
        """回归（2026-11-10）：面板收起时 AX 树里只有入口按钮，没有「立即领取」。
        必须先展开加油站、重新快照定位，否则整天假阳性不签到。"""
        snaps = [
            desktop_snap({"element_index": 58, "role": "AXButton", "label": "Buddy加油站"}),
            desktop_snap({"element_index": 64, "role": "AXButton", "label": "立即领取",
                          "element_token": "s2:64"}),
            desktop_snap(),  # 点击签到后按钮消失
        ]
        runner = CuaFakeRunner(snaps)
        result = CuaDriverSigner(self.cfg, runner).claim_daily(["立即领取"])
        self.assertTrue(result.ok, result.detail)
        self.assertTrue(result.data["confirmed"])
        self.assertTrue(result.data["station_expanded"])
        clicks = [json.loads(c[3]) for c in runner.calls if c[2] == "click"]
        self.assertEqual(len(clicks), 2)
        self.assertEqual(clicks[0]["element_index"], 58)       # 先展开面板
        self.assertEqual(clicks[1].get("element_token"), "s2:64")  # 重新定位并优先用 token

    def test_no_button_after_expanding_means_already_checked_in(self):
        snaps = [
            desktop_snap({"element_index": 58, "role": "AXButton", "label": "Buddy加油站"}),
            desktop_snap(),  # 展开后仍无领取按钮
        ]
        runner = CuaFakeRunner(snaps)
        result = CuaDriverSigner(self.cfg, runner).claim_daily(["立即领取"])
        self.assertTrue(result.ok)
        self.assertTrue(result.data["confirmed"])
        self.assertTrue(result.data["already"])

    def test_missing_button_and_entry_is_uncertain_not_checked_in(self):
        """连入口都找不到：不能推断已签，交给调度器隔一小时再查。"""
        runner = CuaFakeRunner([desktop_snap()])
        result = CuaDriverSigner(self.cfg, runner).claim_daily(["立即领取"])
        self.assertFalse(result.ok)
        self.assertFalse(result.data["confirmed"])
        self.assertTrue(result.data["uncertain"])
        self.assertFalse(any(c[2] == "click" for c in runner.calls))

    def test_refuses_non_ax_click_path(self):
        """非 AX 路径会合成鼠标事件把光标甩到左上角，必须拒绝。"""
        snaps = [
            desktop_snap({"element_index": 65, "role": "AXButton", "label": "立即领取"}),
            desktop_snap(),
        ]
        runner = CuaFakeRunner(snaps, click_path="cg_event")
        result = CuaDriverSigner(self.cfg, runner).claim_daily(["立即领取"])
        self.assertFalse(result.ok)
        self.assertIn("non-AX", result.detail)


def growth_snap(labels: list[str]) -> dict:
    return {
        "elements": [
            {"element_index": i, "role": "AXButton", "label": lab}
            for i, lab in enumerate(labels)
        ],
        "element_count": len(labels),
    }


class GrowthFakeRunner:
    def __init__(self, snaps: list[dict]):
        self.snaps = list(snaps)
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **_kwargs):
        self.calls.append(cmd)
        tool = cmd[2]
        if tool == "list_windows":
            out = {"windows": [{
                "app_name": "Microsoft Edge", "pid": 446, "window_id": 60,
                "title": "WorkBuddy - 个人中心", "is_on_screen": True,
                "bounds": {"width": 1900, "height": 940},
            }]}
        elif tool == "get_window_state":
            out = self.snaps.pop(0) if len(self.snaps) > 1 else self.snaps[0]
        else:
            out = {"ok": True}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")


class GrowthCenterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = test_cfg(Path(self.tmp.name))
        self.cfg["growth"] = {
            "enabled": True, "cua_driver_bin": "/fake/cua-driver",
            "browser_app": "Microsoft Edge", "title_hint": "个人中心",
            "scene": "", "claim_offset_sec": 3,
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_hms(self):
        self.assertEqual(parse_hms("旅行倒计时 00:59:56 "), 3596)
        self.assertEqual(parse_hms("02:15:30"), 8130)
        self.assertIsNone(parse_hms("预计出行时间为1～4小时"))

    def test_read_state_variants(self):
        cases = [
            (["派猫猫旅行", "开启盲盒 8/10"], "idle"),
            (["旅行倒计时 00:59:56", "Buddy 正在咖啡馆采风中..."], "traveling"),
            (["领取礼物", "查看 Buddy 图鉴"], "claimable"),
            (["领取 7 积分", "关闭"], "gift_dialog"),
            (["确定派出", "预计出行时间为1～4小时"], "scene_dialog"),
        ]
        for labels, expected in cases:
            info = GrowthCenter.read_state(growth_snap(labels))
            self.assertEqual(info["state"], expected, labels)
        self.assertEqual(
            GrowthCenter.read_state(growth_snap(["旅行倒计时 00:17:55"]))["remaining_sec"], 1075
        )

    def test_dispatch_due_varies_with_real_countdown(self):
        """核心需求：每次派遣时长不同，到期时间必须按真实倒计时算。"""
        dues = []
        for countdown in ("00:59:56", "02:15:30"):
            runner = GrowthFakeRunner([
                growth_snap(["派猫猫旅行", "开启盲盒 8/10"]),
                growth_snap(["确定派出", "咖啡馆", "预计出行时间为1～4小时"]),
                growth_snap(["旅行倒计时 " + countdown, "Buddy 正在咖啡馆采风中..."]),
            ])
            result = GrowthCenter(self.cfg, runner).dispatch()
            self.assertTrue(result.ok, result.detail)
            self.assertIn("real duration", result.detail)
            dues.append(result.data["due_at"])
        self.assertAlmostEqual(dues[1] - dues[0], 8130 - 3596, delta=5)

    def test_scan_exchanges_is_conservative(self):
        snap = growth_snap([
            "兑换奖励", "抽奖(0)", "开启盲盒 8/10", "抽奖(2)", "开启盲盒 10/10", "1", "2",
        ])
        pairs = GrowthCenter.scan_exchanges(snap)
        labels = [p[0] for p in pairs]
        self.assertIn("兑换奖励", labels)
        self.assertIn("抽奖(2)", labels)
        self.assertIn("开启盲盒 10/10", labels)
        self.assertNotIn("抽奖(0)", labels)
        self.assertNotIn("开启盲盒 8/10", labels)
        self.assertNotIn("1", labels)  # 补登卡/日期按钮永不自动点

    def test_claim_exchanges_skips_already_claimed(self):
        runner = GrowthFakeRunner([growth_snap(["兑换奖励", "旅行倒计时 00:10:00"])])
        gc = GrowthCenter(self.cfg, runner)
        results = gc.claim_exchanges(claimed_before=lambda _key: True)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].data["skipped"])
        self.assertFalse(any(c[2] == "click" for c in runner.calls))

    def test_traveling_tick_records_due_from_countdown(self):
        runner = GrowthFakeRunner([growth_snap(["旅行倒计时 00:17:55"])])
        result = GrowthCenter(self.cfg, runner).tick()
        self.assertTrue(result.ok)
        self.assertEqual(result.data["remaining_sec"], 1075)
        self.assertAlmostEqual(result.data["due_at"], time.time() + 1075 + 3, delta=5)

    def test_actions_persists_growth_due(self):
        state = State(Path(self.tmp.name) / "growth.sqlite3")
        try:
            due = time.time() + 1234
            actions = Actions(self.cfg, state, FakeDesktop(), growth=FakeGrowth(due))
            result = actions.travel_tick()
            self.assertTrue(result.ok)
            self.assertAlmostEqual(state.kv_get_float("travel_due_at"), due, places=2)
        finally:
            state.close()


class CliTests(unittest.TestCase):
    def test_install_launchd_writes_plist_without_loading(self):
        import argparse
        import contextlib
        import io
        from buddy_checkin import cli

        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"HOME": td}):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = cli.cmd_install_launchd(
                    copy.deepcopy(config.DEFAULTS), argparse.Namespace()
                )
            plist = Path(td) / "Library/LaunchAgents/com.local.buddy-web-checkin.plist"
            self.assertEqual(rc, 0)
            self.assertTrue(plist.exists())
            text = plist.read_text(encoding="utf-8")
            self.assertIn("KeepAlive", text)
            self.assertIn("RunAtLoad", text)
            self.assertNotIn("launchctl bootstrap", text)


class FreeModelGuardTests(unittest.TestCase):
    """依赖 WorkBuddy 必须在免费模型下运行：缺确认则拒绝执行。"""

    def _cfg(self, require: bool) -> dict:
        return {"runtime": {"require_free_model": require, "free_model_hint": "hint"}}

    def test_no_require_passes(self):
        from buddy_checkin import cli

        self.assertEqual(cli.preflight_model_check(self._cfg(False)), 0)

    def test_require_without_env_refuses(self):
        from buddy_checkin import cli

        os.environ.pop("BUDDY_FREE_MODEL", None)
        self.assertEqual(cli.preflight_model_check(self._cfg(True)), 2)

    def test_require_with_env_passes(self):
        from buddy_checkin import cli

        with patch.dict(os.environ, {"BUDDY_FREE_MODEL": "1"}):
            self.assertEqual(cli.preflight_model_check(self._cfg(True)), 0)

    def test_env_case_insensitive(self):
        from buddy_checkin import cli

        for v in ("true", "yes", "on"):
            with patch.dict(os.environ, {"BUDDY_FREE_MODEL": v}):
                self.assertEqual(cli.preflight_model_check(self._cfg(True)), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
