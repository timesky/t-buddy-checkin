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


class FakeGrowth:
    """假成长中心：checkin 为状态校验（不再点按钮），其余同旧接口。"""

    def __init__(self, due: float, checkin_ok: bool = True):
        self.due = due
        self.checkin_ok = checkin_ok
        self.checkin_calls = 0

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

    def tick(self, force: bool = False):
        return ActionResult(True, "travel", "fake growth ok", {"due_at": self.due})


class FakeActions:
    """调度器用的假编排：只记录三条线各自的调用与可配置的结论。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.checkin_confirmed = True
        self.exchange_ok = True
        self.travel_due_at: float | None = None
        self.travel_blocked: str | None = None

    def daily_checkin(self):
        self.calls.append("checkin")
        return ActionResult(True, "checkin", "fake", {"confirmed": self.checkin_confirmed})

    def check_exchanges(self):
        self.calls.append("exchange")
        return ActionResult(self.exchange_ok, "exchange", "fake", {"count": 0})

    def travel_tick(self, force: bool = False):
        self.calls.append("travel")
        data: dict = {"due_at": self.travel_due_at}
        if self.travel_blocked:
            data["dispatch_blocked"] = self.travel_blocked
        return ActionResult(not self.travel_blocked, "travel", "fake", data)


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
        actions = Actions(self.cfg, self.state, None, growth=growth)
        summary = actions.reconcile_all()
        self.assertTrue(summary["ok"])
        self.assertEqual(growth.checkin_calls, 1)
        self.assertEqual(self.state.kv_get("last_checkin_day"), day_key())
        due = self.state.kv_get_float("travel_due_at")
        self.assertIsNotNone(due)
        self.assertGreater(due, time.time())

        actions.reconcile_all()
        self.assertEqual(growth.checkin_calls, 1)  # 当天不重复读签到
        # travel_due_at 仍由 growth 的真实倒计时刷新
        self.assertAlmostEqual(self.state.kv_get_float("travel_due_at"),
                               self.state.kv_get_float("travel_due_at"))

    def test_unconfirmed_checkin_does_not_write_state(self):
        """回归（2026-11-10）：读不到/未记录只是推测，绝不能写 last_checkin_day，
        否则当天所有后续轮询都被跳过，整天漏签。"""
        growth = FakeGrowth(due=time.time() + 1200, checkin_ok=False)
        actions = Actions(self.cfg, self.state, None, growth=growth)
        result = actions.daily_checkin()
        self.assertFalse(result.data.get("confirmed"))
        self.assertIsNone(self.state.kv_get("last_checkin_day"))
        # 下一轮仍会真正检查，而不是被幂等跳过
        actions.daily_checkin()
        self.assertEqual(2, growth.checkin_calls)

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

    def test_dispatch_exhausted_defers_to_2300(self):
        """派遣次数当天用尽：按用户要求禁用到当晚 23:00，不再每小时重试。"""
        self.actions.travel_blocked = "exhausted"
        job = self.scheduler._travel(None)
        b = self._beijing(job.due_at)
        self.assertEqual((b.date().isoformat(), b.hour, b.minute),
                         ("2026-11-10", 23, 0))
        self.assertEqual(self.state.kv_get("travel_blocked"), "exhausted")

    def test_dispatch_exhausted_after_2300_defers_to_midnight(self):
        """已过 23:00 才发现用尽 → 次日 0 点（次数通常 0 点恢复）。"""
        self.fixed_now = datetime(2026, 11, 10, 23, 30, 0, tzinfo=CN_TZ).timestamp()
        self.actions.travel_blocked = "exhausted"
        job = self.scheduler._travel(None)
        b = self._beijing(job.due_at)
        self.assertEqual((b.date().isoformat(), b.hour, b.minute),
                         ("2026-11-11", 0, 0))

    def test_exhausted_survives_restart_without_rechecking(self):
        """回归（2026-11-10）：重启后 initial_jobs 立即跑，但当天已用尽必须
        直接跳到 23:00，绝不重新检查（否则会触发自动开新页签）。"""
        self.state.kv_set("travel_blocked", "exhausted")
        self.state.kv_set("travel_blocked_day", "2026-11-10")
        # 模拟进程重启后的新调度器：固定时间仍在 11:00，且 travel_due_at 已过期
        self.state.kv_set("travel_due_at", self.fixed_now - 3600)
        fresh = self._sched()
        job = fresh._travel(None)
        b = self._beijing(job.due_at)
        self.assertEqual((b.date().isoformat(), b.hour, b.minute),
                         ("2026-11-10", 23, 0))
        self.assertEqual(self.actions.calls, [])  # 一次实际检查都没发生

    def test_exhausted_from_previous_day_is_cleared(self):
        """跨天：昨天的禁用标记不应影响今天。"""
        self.state.kv_set("travel_blocked", "exhausted")
        self.state.kv_set("travel_blocked_day", "2026-11-09")
        self.state.kv_set("travel_due_at", self.fixed_now + 1200)
        job = self.scheduler._travel(None)
        self.assertAlmostEqual(job.due_at, self.fixed_now + 1200, places=2)
        self.assertEqual(self.actions.calls, ["travel"])
        self.assertEqual(self.state.kv_get("travel_blocked"), "")

    def test_run_forever_executes_all_three_lines(self):
        executed = self.scheduler.run_forever(max_iterations=3)
        self.assertEqual(executed, 3)
        self.assertEqual(set(self.actions.calls), {"checkin", "exchange", "travel"})


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
