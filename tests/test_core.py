"""全链路单测：纯标准库，不启动浏览器、不碰桌面 GUI。"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from buddy_checkin import config  # noqa: E402
from buddy_checkin.actions import Actions  # noqa: E402
from buddy_checkin.adapters.base import ActionResult, DesktopAdapter, WebAdapter  # noqa: E402
from buddy_checkin.adapters.desktop import CuaDriverSigner, DesktopSigner  # noqa: E402
from buddy_checkin.adapters.growth import GrowthCenter, parse_hms  # noqa: E402
from buddy_checkin.adapters.web import AgentBrowser, scrub_text  # noqa: E402
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
    cfg["web"]["profile"] = str(tmp / "profile")
    cfg["state_db"] = str(tmp / "state.sqlite3")
    cfg["web"]["status_endpoint"] = ""
    return cfg


class FakeRunner:
    def __init__(self, outputs: dict[str, str] | None = None, returncode: int = 0):
        self.calls: list[list[str]] = []
        self.outputs = outputs or {}
        self.returncode = returncode

    def __call__(self, cmd, **_kwargs):
        self.calls.append(cmd)
        out = ""
        for needle, value in self.outputs.items():
            if any(needle in str(part) for part in cmd):
                out = value
                break
        return subprocess.CompletedProcess(cmd, self.returncode, stdout=out, stderr="")


class FakeWeb(WebAdapter):
    def __init__(self, snapshots: list[str]):
        self.snapshots = list(snapshots) or [""]
        self.clicks: list[list[str]] = []
        self.opens = 0
        self.waits = 0

    def snapshot(self) -> str:
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]

    def eval_js(self, js: str) -> str:
        return "FETCH_FAILED"

    def click_text(self, texts: list[str]) -> bool:
        self.clicks.append(texts)
        return True

    def get_url(self) -> str:
        return "https://www.workbuddy.cn/app"

    def screenshot(self, path: str) -> str:
        return path

    def wait(self, ms: int = 3000) -> None:
        self.waits += 1

    def close(self) -> None:
        pass

    def open(self, url: str | None = None) -> ActionResult:
        self.opens += 1
        return ActionResult(True, "web.open", "")


class FakeDesktop(DesktopAdapter):
    def __init__(self) -> None:
        self.calls = 0

    def probe(self) -> ActionResult:
        return ActionResult(True, "desktop.probe", "")

    def claim_daily(self, texts: list[str]) -> ActionResult:
        self.calls += 1
        return ActionResult(True, "checkin", "ok")


class FakeActions:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def reconcile_all(self, reason: str = "manual") -> dict:
        self.calls.append(reason)
        return {}

    def travel_tick(self, force: bool = False) -> ActionResult:
        self.calls.append("travel")
        return ActionResult(True, "travel", "")


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
        self.assertEqual(cfg["web"]["engine"], "agent-browser")
        self.assertTrue(cfg["web"]["headless"])
        self.assertFalse(cfg["desktop"]["allow_focus"])
        scrubbed = config.scrub({"accessToken": "abc", "nested": {"cookie": "x", "ok": 1}})
        self.assertEqual(scrubbed["accessToken"], "<redacted>")
        self.assertEqual(scrubbed["nested"]["cookie"], "<redacted>")
        self.assertEqual(scrubbed["nested"]["ok"], 1)

    def test_toml_merge(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            path.write_text('[web]\ntimeout_sec = 99\n', encoding="utf-8")
            merged = config.load(path)
            self.assertEqual(merged["web"]["timeout_sec"], 99)
            self.assertEqual(merged["web"]["session"], "buddy-checkin")


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


class WebAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = test_cfg(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_commands_are_isolated_and_headless(self):
        runner = FakeRunner()
        web = AgentBrowser(self.cfg, runner)
        web.open()
        web.snapshot()
        web.close()
        for cmd in web.commands:
            self.assertIn("--session", cmd)
            self.assertIn("--profile", cmd)
            self.assertNotIn("--headed", cmd)
        self.assertEqual(web.commands[0][-2:], ["open", "https://www.workbuddy.cn/app"])

    def test_click_text_tries_candidates(self):
        runner = FakeRunner()
        web = AgentBrowser(self.cfg, runner)
        self.assertTrue(web.click_text(["立即领取", "领取今日礼包"]))
        self.assertEqual(runner.calls[0][-4:], ["find", "text", "立即领取", "click"])

    def test_scrub_text(self):
        self.assertIn("<redacted>", scrub_text("Authorization: Bearer abc.def"))
        self.assertIn("normal line", scrub_text("normal line"))

    def test_build_includes_custom_executable(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["web"]["executable_path"] = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
        web = AgentBrowser(cfg, FakeRunner())
        cmd = web.build(["open", "https://www.workbuddy.cn/app"])
        self.assertIn("--executable-path", cmd)
        self.assertIn("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", cmd)


def ax_fixtures(accessibility: str = "granted") -> dict[str, str]:
    """按 agent-desktop 1.2.0 真实 JSON envelope 构造，不用臆造的纯文本。"""
    return {
        "permissions": json.dumps({
            "version": "2.6", "ok": True, "command": "permissions",
            "data": {"accessibility": {"state": accessibility},
                     "screen_recording": {"state": "granted"},
                     "automation": {"state": "unknown"}},
        }),
        "snapshot": json.dumps({
            "version": "2.6", "ok": True, "command": "snapshot",
            "data": {"snapshot_id": "s1a2b3", "elements": [
                {"ref": "@s1a2b3:e7", "role": "button", "name": "立即领取"},
                {"ref": "@s1a2b3:e8", "role": "button", "name": "成长计划"},
            ]},
        }),
        "click": json.dumps({
            "version": "2.6", "ok": True, "command": "click",
            "data": {"delivery": "semantic"},
        }),
    }


class DesktopAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = test_cfg(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_probe_is_read_only(self):
        runner = FakeRunner(ax_fixtures())
        with patch("buddy_checkin.adapters.desktop.shutil.which", return_value="/usr/local/bin/agent-desktop"):
            result = DesktopSigner(self.cfg, runner).probe()
        self.assertTrue(result.ok)
        self.assertEqual(result.data["chosen"], "agent-desktop")
        self.assertEqual(result.data["checkin_ref"], "@s1a2b3:e7")
        for cmd in runner.calls:
            self.assertNotIn("click", cmd)
            self.assertNotIn("--activate", cmd)
            self.assertNotIn("--headed", cmd)
        snap_cmds = [c for c in runner.calls if "snapshot" in c]
        self.assertTrue(snap_cmds)
        self.assertIn("--app", snap_cmds[0])
        self.assertIn("--force-electron-a11y", snap_cmds[0])

    def test_claim_daily_uses_qualified_ref_and_no_app_flag(self):
        runner = FakeRunner(ax_fixtures())
        with patch("buddy_checkin.adapters.desktop.shutil.which", return_value="/usr/local/bin/agent-desktop"):
            result = DesktopSigner(self.cfg, runner).claim_daily(["立即领取", "今日礼包"])
        self.assertTrue(result.ok)
        self.assertEqual(result.data["ref"], "@s1a2b3:e7")
        click_cmds = [c for c in runner.calls if "click" in c]
        self.assertEqual(len(click_cmds), 1)
        self.assertIn("@s1a2b3:e7", click_cmds[0])
        self.assertNotIn("--app", click_cmds[0])  # 真实 CLI 的 click 没有 --app
        self.assertIn("--session", click_cmds[0])

    def test_permission_denied_is_reported_and_no_snapshot_wasted(self):
        runner = FakeRunner(ax_fixtures("denied"))
        with patch("buddy_checkin.adapters.desktop.shutil.which", return_value="/usr/local/bin/agent-desktop"):
            result = DesktopSigner(self.cfg, runner).probe()
        self.assertFalse(result.ok)
        self.assertIn("Accessibility permission denied", result.detail)
        self.assertFalse(any("snapshot" in c for c in runner.calls))

    def test_envelope_ok_false_beats_returncode(self):
        fixtures = ax_fixtures()
        fixtures["snapshot"] = json.dumps({
            "version": "2.6", "ok": False, "command": "snapshot",
            "error": {"code": "PERM_DENIED", "message": "Accessibility permission not granted"},
        })
        runner = FakeRunner(fixtures)  # returncode 仍是 0
        with patch("buddy_checkin.adapters.desktop.shutil.which", return_value="/usr/local/bin/agent-desktop"):
            result = DesktopSigner(self.cfg, runner).claim_daily(["立即领取"])
        self.assertFalse(result.ok)
        self.assertIn("PERM_DENIED", result.detail)

    def test_no_backend_does_not_touch_mouse(self):
        with patch("buddy_checkin.adapters.desktop.shutil.which", return_value=None), patch(
            "buddy_checkin.adapters.desktop.socket.create_connection", side_effect=OSError
        ):
            signer = DesktopSigner(self.cfg, FakeRunner())
            self.assertFalse(signer.probe().ok)
            self.assertFalse(signer.claim_daily(["立即领取"]).ok)


class ActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = test_cfg(Path(self.tmp.name))
        self.state = State(Path(self.tmp.name) / "state.sqlite3")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_full_loop_is_idempotent(self):
        web = FakeWeb(["派猫猫旅行 按钮", "旅行中 倒计时 00:17:55"])
        desktop = FakeDesktop()
        actions = Actions(self.cfg, self.state, web, desktop)
        summary = actions.reconcile_all()
        self.assertTrue(summary["ok"])
        self.assertEqual(desktop.calls, 1)
        self.assertEqual(self.state.kv_get("last_checkin_day"), day_key())
        due = self.state.kv_get_float("travel_due_at")
        self.assertIsNotNone(due)
        self.assertGreater(due, time.time())
        clicks_after_first = len(web.clicks)

        actions.reconcile_all()
        self.assertEqual(desktop.calls, 1)  # 当天不重复签到
        # 礼物已领取过，第二次不再产生新的 gift 点击
        gift_again = [c for c in web.clicks[clicks_after_first:] if "领取" in "".join(c)]
        self.assertEqual(gift_again, [])

    def test_force_claim_then_redispatch(self):
        web = FakeWeb(["领取旅行奖励 按钮 旅行中 倒计时 00:00:03"])
        actions = Actions(self.cfg, self.state, web, FakeDesktop())
        result = actions.travel_tick(force=True)
        self.assertTrue(result.ok)
        self.assertTrue(result.data["re_dispatched"])
        joined = " ".join(" ".join(c) for c in web.clicks)
        self.assertIn("领取旅行奖励", joined)
        self.assertIn("派猫猫旅行", joined)
        self.assertIsNotNone(self.state.kv_get_float("travel_due_at"))

    def test_reconcile_reopens_empty_web_session(self):
        """真实 bug 回归：session 重开是空白页时，reconcile_all 必须显式 open + wait。"""
        class EmptyThenLoaded(FakeWeb):
            def snapshot(self) -> str:
                if self.opens == 0:
                    return "(no interactive elements)"
                return "派猫猫旅行 按钮 旅行中 倒计时 00:17:55"

        web = EmptyThenLoaded(["(no interactive elements)"])
        actions = Actions(self.cfg, self.state, web, FakeDesktop())
        actions.reconcile_all()
        self.assertEqual(web.opens, 1)
        self.assertEqual(web.waits, 1)

    def test_login_required_is_reported_not_swallowed(self):
        """未登录必须明确报「未登录」，不能混成 travel state unknown。"""
        web = FakeWeb(["微信扫码登录 Iframe wechat-qrcode 其他登录方式"])
        actions = Actions(self.cfg, self.state, web, FakeDesktop())
        summary = actions.reconcile_all()
        self.assertIn("未登录", summary["gifts"][0]["detail"])
        self.assertIn("未登录", summary["travel"]["detail"])
        self.assertFalse(summary["ok"])


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = test_cfg(Path(self.tmp.name))
        self.state = State(Path(self.tmp.name) / "state.sqlite3")
        self.actions = FakeActions()

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_initial_jobs_include_daily_and_travel(self):
        jobs = Scheduler(self.cfg, self.state, self.actions).initial_jobs(time.time())
        self.assertEqual({j.name for j in jobs}, {"daily", "travel"})

    def test_overdue_due_is_clamped_for_catch_up(self):
        now = time.time()
        self.state.kv_set("travel_due_at", now - 7200)
        jobs = Scheduler(self.cfg, self.state, self.actions).initial_jobs(now)
        travel = next(j for j in jobs if j.name == "travel")
        self.assertAlmostEqual(travel.due_at, now, places=2)

    def test_run_forever_executes_without_real_sleep(self):
        scheduler = Scheduler(
            self.cfg, self.state, self.actions, sleep=lambda _s: None
        )
        executed = scheduler.run_forever(max_iterations=2)
        self.assertEqual(executed, 2)
        self.assertTrue(any(c == "daily" for c in self.actions.calls))
        self.assertIn("travel", self.actions.calls)


class CuaFakeRunner:
    """模拟 cua-driver call：第一次快照有签到按钮，点击后快照里按钮消失。"""

    def __init__(self, button_present: bool = True):
        self.calls: list[list[str]] = []
        self.snap_count = 0
        self.button_present = button_present

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
            elements = [{"element_index": 0, "role": "AXWindow", "label": "WorkBuddy"}]
            if self.button_present and self.snap_count == 1:
                elements.append({"element_index": 65, "role": "AXButton", "label": "立即领取"})
            out = {"elements": elements, "element_count": len(elements)}
        elif tool == "click":
            out = {"effect": "unverifiable", "path": "ax", "verified": False}
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
        runner = CuaFakeRunner()
        result = CuaDriverSigner(self.cfg, runner).claim_daily(["立即领取"])
        self.assertTrue(result.ok)
        self.assertTrue(result.data["verified"])
        clicks = [c for c in runner.calls if c[2] == "click"]
        self.assertEqual(len(clicks), 1)
        payload = json.loads(clicks[0][3])
        self.assertEqual(payload, {"pid": 452, "window_id": 66, "element_index": 65})
        self.assertEqual(runner.snap_count, 2)  # 点击后复核快照

    def test_absent_button_means_already_checked_in(self):
        runner = CuaFakeRunner(button_present=False)
        result = CuaDriverSigner(self.cfg, runner).claim_daily(["立即领取"])
        self.assertTrue(result.ok)
        self.assertTrue(result.data["already"])
        self.assertFalse(any(c[2] == "click" for c in runner.calls))


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


class FakeGrowth:
    def __init__(self, due: float):
        self.due = due

    def claim_gift(self):
        return ActionResult(True, "gift", "ok")

    def tick(self, force: bool = False):
        return ActionResult(True, "travel", "fake growth ok", {"due_at": self.due})


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
            actions = Actions(self.cfg, state, FakeWeb(["idle"]), FakeDesktop(), growth=FakeGrowth(due))
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
        import os
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

    def test_copy_edge_profile(self):
        from buddy_checkin import cli

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            src, dst = base / "edge", base / "copy"
            (src / "Default" / "Local Storage").mkdir(parents=True)
            con = sqlite3.connect(str(src / "Default" / "Cookies"))
            con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB)")
            con.execute("INSERT INTO cookies VALUES (?, ?, ?)",
                        ("www.workbuddy.cn", "session", b"v10dummy"))
            con.commit()
            con.close()
            (src / "Default" / "Preferences").write_text("{}", encoding="utf-8")
            (src / "Local State").write_text("{}", encoding="utf-8")

            report = cli.copy_edge_profile(src, dst)
            self.assertIn("Cookies", report["copied"])
            self.assertTrue((dst / "Default" / "Cookies").exists())
            con = sqlite3.connect(str(dst / "Default" / "Cookies"))
            names = [r[0] for r in con.execute("SELECT name FROM cookies").fetchall()]
            con.close()
            self.assertEqual(names, ["session"])
            self.assertTrue((dst / "Local State").exists())
            self.assertTrue((src / "Default" / "Cookies").exists())  # 源 profile 不动


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
