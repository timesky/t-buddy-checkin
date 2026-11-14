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
from buddy_checkin.progress import Progress  # noqa: E402
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
    # 单测不得触发真实的「自动打开页面 / open -g」
    cfg["growth"]["auto_open_page"] = False
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


class FakeFuel:
    """假「Buddy加油站」浮层：签到的唯一真实通道（2026-11-14 更正）。"""

    def __init__(self, confirmed: bool = True):
        self.confirmed = confirmed
        self.claim_calls = 0

    def claim(self):
        self.claim_calls += 1
        if self.confirmed:
            return ActionResult(True, "checkin", "claimed: 领取成功",
                                {"confirmed": True, "source": "fuel-card"})
        return ActionResult(False, "checkin", "fuel card not rendered",
                            {"confirmed": False, "source": "fuel-card"})


class FakeGrowth:
    """假成长中心：礼物/派遣/兑换/补签/抽奖；checkin 仅保留为活跃地图读取。"""

    def __init__(self, due: float, checkin_ok: bool = True):
        self.due = due
        self.checkin_ok = checkin_ok
        self.checkin_calls = 0
        self.makeup_state = "done"
        self.lottery_state = "done"

    def checkin(self):
        self.checkin_calls += 1
        if self.checkin_ok:
            return ActionResult(True, "checkin", "today recorded in heatmap",
                                {"confirmed": True, "checked": True, "streak": 10})
        return ActionResult(False, "checkin", "today NOT recorded", {"confirmed": False})

    def claim_gift(self):
        return ActionResult(True, "gift", "ok")

    def claim_exchanges(self, claimed_before):
        return []

    def makeup(self):
        if self.makeup_state == "done":
            return ActionResult(True, "makeup", "no missed day", {"state": "done"})
        if self.makeup_state == "impossible":
            return ActionResult(True, "makeup", "no makeup card left",
                                {"state": "impossible"})
        return ActionResult(False, "makeup", "tip not shown", {"state": "pending"})

    def lottery(self):
        if self.lottery_state == "done":
            return ActionResult(True, "lottery", "no lottery ticket",
                                {"state": "done", "tickets": 0})
        return ActionResult(False, "lottery", "dialog not found", {"state": "pending"})

    def tick(self, force: bool = False):
        return ActionResult(True, "travel", "fake growth ok", {"due_at": self.due})


class FakeActions:
    """调度器用的假编排：五个任务各自可配置结论。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.states: dict[str, str] = {}
        self.travel_due_at: float | None = None

    def _r(self, name: str, default: str = "done"):
        self.calls.append(name)
        state = self.states.get(name, default)
        return state

    def daily_checkin(self):
        state = self._r("checkin")
        return ActionResult(state == "done", "checkin", "fake",
                            {"confirmed": state == "done", "state": state})

    def missed_makeup(self):
        state = self._r("makeup")
        return ActionResult(state != "pending", "makeup", "fake", {"state": state})

    def check_exchanges(self):
        state = self._r("exchange")
        return ActionResult(state == "done", "exchange", "fake",
                            {"count": 0, "state": state})

    def draw_lottery(self):
        state = self._r("lottery")
        return ActionResult(state == "done", "lottery", "fake", {"state": state})

    def travel_tick(self, force: bool = False):
        state = self._r("travel", "pending")
        data: dict = {"due_at": self.travel_due_at, "state": state}
        return ActionResult(state == "done", "travel", "fake", data)


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
        # 默认引擎是 CDP，且指向客户端的 9222 调试端口
        self.assertEqual(cfg["growth"]["engine"], "browser")
        self.assertEqual(cfg["growth"]["cdp_port"], 9222)
        # 旧 CuaDriver / AX 通道的配置键必须已清除
        self.assertNotIn("cua_driver_bin", cfg["desktop"])
        self.assertNotIn("cua_driver_bin", cfg["growth"])
        self.assertNotIn("dom_fallback", cfg)
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
        fuel = FakeFuel()
        actions = Actions(self.cfg, self.state, None, growth=growth, fuel=fuel)
        summary = actions.reconcile_all()
        self.assertEqual(summary["checkin"]["state"], "done")
        self.assertEqual(summary["makeup"]["state"], "done")
        self.assertEqual(summary["lottery"]["state"], "done")
        # 派遣是滚动任务：还有倒计时没走完，当天没结束
        self.assertEqual(summary["travel"]["state"], "pending")
        self.assertFalse(summary["ok"])
        self.assertEqual(fuel.claim_calls, 1)
        self.assertEqual(self.state.kv_get("last_checkin_day"), day_key())
        due = self.state.kv_get_float("travel_due_at")
        self.assertIsNotNone(due)
        self.assertGreater(due, time.time())

        second = actions.reconcile_all()
        # 第二轮仍会向服务端复核一次签到（不盲信本地文件），但结果仍必须是 done
        self.assertEqual(second["checkin"]["state"], "done")
        self.assertEqual(self.state.kv_get("last_checkin_day"), day_key())

    def test_unconfirmed_checkin_does_not_write_state(self):
        """回归（2026-11-10 / 2026-11-14）：浮层没领到只是「没证据」，绝不能写
        last_checkin_day，否则当天所有后续轮询都被跳过，整天漏签。"""
        fuel = FakeFuel(confirmed=False)
        actions = Actions(self.cfg, self.state, None,
                          growth=FakeGrowth(time.time() + 1200), fuel=fuel)
        result = actions.daily_checkin()
        self.assertFalse(result.ok)
        self.assertFalse(result.data.get("confirmed"))
        self.assertIsNone(self.state.kv_get("last_checkin_day"))
        # 下一轮仍会真正重试，而不是被幂等跳过
        actions.daily_checkin()
        self.assertEqual(2, fuel.claim_calls)

    def test_checkin_never_uses_heatmap_as_proof(self):
        """回归（2026-11-14）：活跃地图绿了不代表签到领到了 —— 之前就是这样
        报 confirmed=True，导致签到从没真正执行。heatmap 只能是参考。"""
        growth = FakeGrowth(time.time() + 1200)   # heatmap 会返回 confirmed=True
        fuel = FakeFuel(confirmed=False)
        actions = Actions(self.cfg, self.state, None, growth=growth, fuel=fuel)
        result = actions.daily_checkin()
        self.assertFalse(result.ok)
        self.assertEqual(0, growth.checkin_calls, "签到不该再去读活跃地图")
        self.assertIsNone(self.state.kv_get("last_checkin_day"))

    def test_force_claim_then_redispatch_records_due(self):
        due = time.time() + 3
        actions = Actions(self.cfg, self.state, None, growth=FakeGrowth(due))
        result = actions.travel_tick(force=True)
        self.assertTrue(result.ok)
        self.assertAlmostEqual(self.state.kv_get_float("travel_due_at"), due, places=2)

    def test_missing_growth_channel_is_reported(self):
        """growth 未启用时必须明确报错，不能静默吞掉。"""
        actions = Actions(self.cfg, self.state, None, growth=None)
        travel = actions.travel_tick()
        self.assertFalse(travel.ok)
        self.assertIn("growth channel not configured", travel.detail)
        gifts = actions.claim_gifts()
        self.assertEqual(len(gifts), 1)
        self.assertFalse(gifts[0].ok)
        self.assertIn("growth channel not configured", gifts[0].detail)


class SchedulerTests(unittest.TestCase):
    """五任务调度 + 当日账本：失败重试、全终态静默、重启强制复核。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = test_cfg(Path(self.tmp.name))
        self.state = State(Path(self.tmp.name) / "state.sqlite3")
        self.actions = FakeActions()
        self.progress_path = Path(self.tmp.name) / "progress.json"
        # 固定为北京时间 2026-11-10 11:00:00
        self.fixed_now = datetime(2026, 11, 10, 11, 0, 0, tzinfo=CN_TZ).timestamp()
        self.slept: list[float] = []
        self.progress = Progress(self.progress_path)
        self.scheduler = self._sched()

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def _sched(self, progress=None):
        return Scheduler(
            self.cfg, self.state, self.actions, progress or self.progress,
            now=lambda: self.fixed_now,
            sleep=lambda s: self.slept.append(s),
        )

    @staticmethod
    def _beijing(ts):
        return datetime.fromtimestamp(ts, CN_TZ)

    def test_startup_runs_all_five_tasks_immediately(self):
        for name in ("checkin", "makeup", "exchange", "lottery", "travel"):
            self.actions.states[name] = "done"
        self.scheduler.run_forever(max_iterations=5)
        self.assertEqual(self.actions.calls,
                         ["checkin", "makeup", "exchange", "lottery", "travel"])

    def test_restart_ignores_silence_and_rechecks_everything(self):
        """重启不受静默控制：账本写着今天全做完并静默，也要立刻复核。"""
        for name in ("checkin", "makeup", "exchange", "lottery", "travel"):
            self.progress.mark(name, "done", "上一进程写的")
            self.actions.states[name] = "done"
        self.progress.mark_silent(self.fixed_now + 3600)
        fresh = Progress(self.progress_path)
        sched = self._sched(fresh)
        sched.run_forever(max_iterations=5)
        self.assertEqual(len(self.actions.calls), 5, "静默状态也必须重新复核五个任务")

    def test_wrong_ledger_is_corrected_on_restart(self):
        """账本写 done 但真实状态是没做 → 复核后必须改回 pending。"""
        self.progress.mark("checkin", "done", "错误的 done")
        self.actions.states["checkin"] = "pending"
        states = self.scheduler.verify_pass()
        self.assertEqual(states["checkin"], "pending")
        self.assertEqual(self.progress.get("checkin").state, "pending")
        self.assertFalse(self.progress.all_terminal())
        self.assertIsNone(self.progress.silent_until)

    def test_failed_task_retries_with_backoff(self):
        self.actions.states["checkin"] = "pending"
        state, _, next_at = self.scheduler.run_task("checkin")
        self.assertEqual(state, "pending")
        self.assertAlmostEqual(next_at - self.fixed_now, 60, places=2)
        # 第二次失败退避到 120s
        self.actions.states["checkin"] = "pending"
        _, _, next_at2 = self.scheduler.run_task("checkin")
        self.assertAlmostEqual(next_at2 - self.fixed_now, 120, places=2)

    def test_terminal_task_is_not_rescheduled(self):
        self.actions.states["exchange"] = "done"
        state, _, next_at = self.scheduler.run_task("exchange")
        self.assertEqual(state, "done")
        self.assertIsNone(next_at)
        self.assertEqual(self.progress.get("exchange").state, "done")

    def test_impossible_is_terminal(self):
        self.actions.states["makeup"] = "impossible"
        state, _, next_at = self.scheduler.run_task("makeup")
        self.assertEqual(state, "impossible")
        self.assertIsNone(next_at)
        self.assertTrue(self.progress.is_terminal("makeup"))

    def test_travel_follows_real_countdown(self):
        """派遣按页面真实倒计时滚动，不套固定间隔。"""
        due = self.fixed_now + 2 * 3600 + 17
        self.actions.travel_due_at = due
        self.actions.states["travel"] = "pending"
        _, _, next_at = self.scheduler.run_task("travel")
        self.assertAlmostEqual(next_at, due, places=2)
        self.assertEqual(self._beijing(next_at).second, 17)

    def test_all_terminal_goes_silent_until_2359(self):
        for name in ("checkin", "makeup", "exchange", "lottery", "travel"):
            self.actions.states[name] = "done"
        self.scheduler.verify_pass()
        self.assertTrue(self.progress.all_terminal())
        silent = self.progress.silent_until
        self.assertIsNotNone(silent)
        b = self._beijing(silent)
        self.assertEqual((b.date().isoformat(), b.hour, b.minute), ("2026-11-10", 23, 59))

    def test_one_pending_task_prevents_silence(self):
        for name in ("checkin", "makeup", "exchange", "lottery", "travel"):
            self.actions.states[name] = "done"
        self.actions.states["lottery"] = "pending"
        self.scheduler.verify_pass()
        self.assertFalse(self.progress.all_terminal())
        self.assertIsNone(self.progress.silent_until)

    def test_verify_pass_runs_tasks_in_dependency_order(self):
        for name in ("checkin", "makeup", "exchange", "lottery", "travel"):
            self.actions.states[name] = "done"
        self.scheduler.verify_pass()
        self.assertEqual(self.actions.calls,
                         ["checkin", "makeup", "exchange", "lottery", "travel"])

    def test_run_forever_keeps_pending_task_scheduled(self):
        for name in ("checkin", "makeup", "exchange", "lottery"):
            self.actions.states[name] = "done"
        self.actions.states["travel"] = "pending"
        self.actions.travel_due_at = self.fixed_now + 600
        executed = self.scheduler.run_forever(max_iterations=6)
        self.assertEqual(executed, 6)
        self.assertEqual(self.actions.calls.count("travel"), 2)


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "progress.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_pending_until_terminal(self):
        p = Progress(self.path)
        self.assertFalse(p.all_terminal())
        for name in ("checkin", "makeup", "exchange", "lottery", "travel"):
            p.mark(name, "done", "ok")
        self.assertTrue(p.all_terminal())

    def test_day_rollover_resets_everything(self):
        p = Progress(self.path, day="2026-11-10")
        p.mark("checkin", "done", "ok")
        p.mark_silent(1794672000.0)
        p2 = Progress(self.path, day="2026-11-11")
        self.assertEqual(p2.get("checkin").state, "pending")
        self.assertIsNone(p2.silent_until)

    def test_corrupted_file_is_ignored(self):
        self.path.write_text("{not json", encoding="utf-8")
        p = Progress(self.path)
        self.assertFalse(p.all_terminal())
        self.assertEqual(p.get("checkin").state, "pending")

    def test_attempts_only_grow_for_pending(self):
        p = Progress(self.path)
        p.mark("checkin", "pending", "no")
        p.mark("checkin", "pending", "no")
        p.mark("checkin", "done", "ok")
        self.assertEqual(p.get("checkin").attempts, 2)


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
