"""常驻进程的任务调度：五个任务各自滚动，全部终态后静默到当天 23:59。

规则（2026-11-14 按用户要求重写）：

1. 五个任务：签到 / 补签 / 兑换 / 抽奖 / 派遣。每个任务独立排程。
2. 失败就重试：pending 的任务按 60s → 120s → 300s → 600s 退避重试，
   直到成功或确认「今天确实做不了」。
3. 静默：**所有**任务都进入 done / impossible（= 当日 100% 完成且无法继续）
   才静默到当天 23:59，期间不再打开任何页面。次日 0 点账本翻页、全部重置。
4. 重启不受静默控制：进程启动时五个任务**立刻各跑一遍**（用真实状态复核，
   不信任账本里写的 done），只有复核后仍全部终态才重新静默。这样即使上一
   进程把状态写错，重启也能立刻纠正。

派遣是唯一按真实倒计时滚动的任务：派生完按页面读到的剩余时间设下一次检查，
领奖后再派，直到页面明确「次数用尽」且没有可领奖励，才记为 done。
"""
from __future__ import annotations

import contextlib
import heapq
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from .progress import DONE, IMPOSSIBLE, PENDING, TASK_LABELS, TASK_ORDER, Progress
from .timeutil import CN_TZ, day_key, next_daily_at

logger = logging.getLogger(__name__)


@dataclass(order=True)
class Job:
    due_at: float
    name: str = field(compare=False)
    fn: Callable[["Job"], "Job | None"] = field(compare=False)

    def run(self) -> "Job | None":
        return self.fn(self)


class Scheduler:
    def __init__(
        self,
        cfg: dict,
        state: Any,
        actions: Any,
        progress: Progress,
        *,
        now: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.state = state
        self.actions = actions
        self.progress = progress
        self.now = now or time.time
        self.sleep = sleep or time.sleep
        sched = cfg.get("schedule", {}) or {}
        self.day_boundary = sched.get("day_boundary", "00:00")
        self.retry_backoff = list(
            sched.get("retry_backoff_sec") or [60, 120, 300, 600])
        self.silent_at = sched.get("silent_at", "23:59")
        self.fallback = float(cfg["travel"].get("fallback_interval_sec", 3600))
        self._next_at: dict[str, float] = {}

    # ---- 时间辅助 -----------------------------------------------------------
    def _today_at(self, hhmm: str) -> float:
        hh, mm = (int(x) for x in hhmm.split(":"))
        dt = datetime.fromtimestamp(self.now(), CN_TZ)
        return dt.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()

    def _next_midnight(self) -> float:
        now_dt = datetime.fromtimestamp(self.now(), CN_TZ)
        return next_daily_at(self.day_boundary, now_dt).timestamp()

    def _backoff(self, attempts: int) -> float:
        if not self.retry_backoff:
            return 300.0
        idx = min(max(attempts - 1, 0), len(self.retry_backoff) - 1)
        return float(self.retry_backoff[idx])

    # ---- 任务执行 -----------------------------------------------------------
    def _dispatch_task(self, name: str):
        return {
            "checkin": self.actions.daily_checkin,
            "makeup": self.actions.missed_makeup,
            "exchange": self.actions.check_exchanges,
            "lottery": self.actions.draw_lottery,
            "travel": self.actions.travel_tick,
        }[name]

    def run_task(self, name: str) -> tuple[str, str, float | None]:
        """跑一个任务，写账本，返回 (state, detail, next_at 或 None 表示今天结束)。"""
        result = self._dispatch_task(name)()
        data = result.data or {}
        # 任务自己给出的 state 是权威值（例如「派遣次数用尽」ok=False 但当天确实
        # 做不了，要记为 done，否则会一直重试）。没有 state 时才按 ok 推断。
        state = str(data.get("state") or (DONE if result.ok else PENDING))
        rec = self.progress.mark(name, state, result.detail, data.get("evidence") or {
            k: v for k, v in data.items() if k in ("today_credit", "streak_days",
                                                   "made_up", "broken_left",
                                                   "tickets_left", "due_at", "state")})

        if state in (DONE, IMPOSSIBLE):
            self.state.kv_set(f"next_{name}_at", None)
            logger.info("%s: %s (%s)", name, state, result.detail[:120])
            return state, result.detail, None

        next_at = None
        if name == "travel":
            due = data.get("due_at")
            if due is not None:
                try:
                    next_at = float(due)
                except (TypeError, ValueError):
                    next_at = None
        if next_at is None:
            next_at = self.now() + self._backoff(rec.attempts)
        logger.info("%s: %s retry in %.0fs (%s)", name, state, next_at - self.now(),
                    result.detail[:120])
        return state, result.detail, next_at

    def _make_job(self, name: str, due: float) -> Job:
        self.state.kv_set(f"next_{name}_at", due)
        return Job(due, name, self._job_runner)

    def _job_runner(self, job: Job) -> "Job | None":
        _, _, next_at = self.run_task(job.name)
        if next_at is None:
            return None
        return self._make_job(job.name, next_at)

    # ---- 启动：不受静默控制，五个任务立刻复核一遍（见 verify_pass） --------
    def verify_pass(self) -> dict[str, str]:
        """重启自检：按顺序把五个任务各跑一遍并写回账本（会真的动作）。

        五个任务共用一次打开的页面（makeup/exchange/lottery/travel 都在成长中心
        里），避免一轮开关好几次窗口。
        """
        out: dict[str, str] = {}
        self._next_at.clear()
        session = getattr(self.actions, "growth_session", None)
        ctx = session() if callable(session) else contextlib.nullcontext()
        with ctx:
            for name in TASK_ORDER:
                try:
                    state, detail, next_at = self.run_task(name)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("verify %s crashed: %s", name, exc)
                    self.progress.mark(name, PENDING, f"crashed: {exc}")
                    out[name] = PENDING
                    self._next_at[name] = self.now() + self._backoff(1)
                    continue
                out[name] = state
                if next_at is not None:
                    self._next_at[name] = next_at
                    self.state.kv_set(f"next_{name}_at", next_at)
        self.progress.mark_silent(self._silence_deadline() if self.progress.all_terminal() else None)
        return out

    def _pending_jobs(self) -> list[Job]:
        """自检后的待办：只排账本里仍 pending 的任务。"""
        jobs: list[Job] = []
        for name in TASK_ORDER:
            if self.progress.is_terminal(name):
                continue
            due = self._next_at.get(name)
            if due is None:
                due = self.now()
            jobs.append(self._make_job(name, max(due, self.now())))
        return jobs

    def _silence_deadline(self) -> float:
        """今天还能静默到什么时候：23:59（当天）。"""
        target = self._today_at(self.silent_at)
        return target if target > self.now() else self._next_midnight()

    def _wake_deadline(self) -> float:
        """静默到 23:59 之后，实际醒来干活的时刻：次日 0 点。"""
        midnight = self._next_midnight()
        if midnight <= self.now():
            midnight += 86400
        return midnight

    # ---- 主循环 -------------------------------------------------------------
    def run_forever(
        self,
        stop: threading.Event | None = None,
        max_iterations: int | None = None,
    ) -> int:
        # 启动即自检：五个任务按顺序各跑一遍（共用一次打开的页面），
        # 不信任账本里写的 done —— 上一进程写错了，这一轮就会纠正。
        states = self.verify_pass()
        logger.info("startup verify: %s", states)
        heap = self._pending_jobs()
        heapq.heapify(heap)
        executed = len(TASK_ORDER)
        while True:
            if stop is not None and stop.is_set():
                break
            if max_iterations is not None and executed >= max_iterations:
                break

            if not heap:
                # 全部终态：静默到今天 23:59（实际睡到次日 0 点再翻页）
                deadline = self._silence_deadline()
                wake = self._wake_deadline()
                self.progress.mark_silent(deadline)
                logger.info(
                    "all tasks terminal for %s; silent until %s, wake at %s",
                    day_key(self.now()),
                    datetime.fromtimestamp(deadline, CN_TZ).strftime("%H:%M:%S"),
                    datetime.fromtimestamp(wake, CN_TZ).strftime("%m-%d %H:%M:%S"))
                if not self._wait(wake - self.now(), stop):
                    break
                self.progress.reset_all()
                self.state.kv_set("silent_until", None)
                logger.info("new day %s: ledger rolled over, running full verify",
                            self.progress.day)
                states = self.verify_pass()
                logger.info("daily verify: %s", states)
                executed += len(TASK_ORDER)
                heap = self._pending_jobs()
                heapq.heapify(heap)
                continue

            job = heapq.heappop(heap)
            wait = job.due_at - self.now()
            if wait > 0:
                if not self._wait(wait, stop):
                    break
            try:
                next_job = job.run()
            except Exception as exc:  # 单条线失败不能拖垮整个进程
                logger.exception("job %s failed: %s", job.name, exc)
                self.state.log(f"scheduler.{job.name}", False, str(exc))
                rec = self.progress.get(job.name)
                self.progress.mark(job.name, PENDING, f"scheduler error: {exc}")
                next_job = self._make_job(
                    job.name, self.now() + self._backoff(rec.attempts + 1))
            executed += 1
            if next_job is not None:
                heapq.heappush(heap, next_job)
        return executed

    def _wait(self, seconds: float, stop: threading.Event | None) -> bool:
        if seconds <= 0:
            return True
        if stop is not None:
            return not stop.wait(seconds)
        self.sleep(seconds)
        return True

    # ---- 只读概览 -----------------------------------------------------------
    def overview(self) -> dict[str, Any]:
        rows = []
        for name in TASK_ORDER:
            rec = self.progress.get(name)
            rows.append({
                "task": name,
                "label": TASK_LABELS.get(name, name),
                "state": rec.state,
                "detail": rec.detail,
                "attempts": rec.attempts,
                "next_at": self.state.kv_get_float(f"next_{name}_at"),
            })
        return {
            "day": self.progress.day,
            "all_terminal": self.progress.all_terminal(),
            "silent_until": self.progress.silent_until,
            "tasks": rows,
        }
