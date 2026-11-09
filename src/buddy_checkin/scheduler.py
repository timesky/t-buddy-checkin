"""精确到期的堆调度器。不依赖 WorkBuddy 定时任务，自带启动补偿。"""
from __future__ import annotations

import heapq
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .timeutil import next_daily_at

logger = logging.getLogger(__name__)


@dataclass(order=True)
class Job:
    due_at: float
    name: str = field(compare=False)
    fn: Callable[["Job"], "Job | None"] = field(compare=False)

    def run(self) -> "Job | None":
        return self.fn(self)


class Scheduler:
    """两条时间线：每日签到 + 旅行到期。执行完的任务返回自己的下一次 Job。"""

    def __init__(
        self,
        cfg: dict,
        state: Any,
        actions: Any,
        *,
        now: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.state = state
        self.actions = actions
        self.now = now or time.time
        self.sleep = sleep or time.sleep

    def _daily(self, _job: Job) -> Job | None:
        self.actions.reconcile_all(reason="daily")
        return Job(
            next_daily_at(self.cfg["daily_checkin"]["at"]).timestamp(),
            "daily",
            self._daily,
        )

    def _travel(self, _job: Job) -> Job | None:
        self.actions.travel_tick(force=True)
        due = self.state.kv_get_float("travel_due_at")
        if due is None or due <= self.now():
            due = self.now() + self.cfg["travel"]["fallback_interval_sec"]
        return Job(due, "travel", self._travel)

    def initial_jobs(self, now_ts: float | None = None) -> list[Job]:
        """过期任务被夹到 now，从而开机/唤醒后立刻补偿漏掉的领取。"""
        now_ts = now_ts if now_ts is not None else self.now()
        jobs: list[Job] = []
        if self.cfg["daily_checkin"]["enabled"]:
            daily_due = next_daily_at(self.cfg["daily_checkin"]["at"]).timestamp()
            last_day = self.state.kv_get("last_checkin_day")
            from .timeutil import day_key

            if self.cfg["daily_checkin"]["catch_up"] and last_day != day_key(now_ts):
                daily_due = min(daily_due, now_ts)
            jobs.append(Job(daily_due, "daily", self._daily))
        if self.cfg["travel"]["enabled"]:
            due = self.state.kv_get_float("travel_due_at")
            if due is None:
                due = now_ts  # 状态未知：立即同步一次
            # 已过期的到期时间夹到 now：启动即补领，不漏掉关机期间的礼物
            jobs.append(Job(max(due, now_ts), "travel", self._travel))
        return jobs

    def run_forever(
        self,
        stop: threading.Event | None = None,
        max_iterations: int | None = None,
    ) -> int:
        heap = self.initial_jobs()
        heapq.heapify(heap)
        executed = 0
        while heap and (max_iterations is None or executed < max_iterations):
            if stop is not None and stop.is_set():
                break
            job = heapq.heappop(heap)
            wait = job.due_at - self.now()
            if wait > 0:
                if stop is not None:
                    # 一次等到期，同时保持可中断（退出/关机不卡死）
                    if stop.wait(wait):
                        break
                else:
                    self.sleep(wait)
            try:
                next_job = job.run()
            except Exception as exc:  # 单个任务失败不能拖垮整个循环
                logger.exception("job %s failed: %s", job.name, exc)
                self.state.log(f"scheduler.{job.name}", False, str(exc))
                next_job = Job(self.now() + 300, job.name, job.fn)
            executed += 1
            if next_job is not None:
                heapq.heappush(heap, next_job)
        return executed
