"""统一常驻进程内的三条独立时间线，用堆精确调度。

进程启动时三条线**都立即执行一次**，然后各自从该时刻向后滚动：

  checkin   每天 0 点恢复；每小时轮询签到状态，*确认*成功后禁用到当天 23:59
            （DB 已记录今日签到 / 点击后按钮消失 = 确认；仅凭按钮不存在推测
             = 未确认，隔一小时再查，避免客户端页面未渲染就整天漏签）
  exchange  每天 0 点恢复；每小时检查兑换，兑换成功或确认无可兑换后禁用到
            当天 23:59；检查失败/窗口不可用则隔一小时重试
  travel    唯一动态滚动：重启先领礼物/派遣，之后按页面真实倒计时设置下次
            检查时间（派遣时长每次不同，绝不套固定间隔）

签到线与兑换线是相对固定的日周期，只有 travel 随真实到期时间动态变化。
每次排 Job 都把下次时刻写入 kv，供 `status` 只读查看。
"""
from __future__ import annotations

import heapq
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .timeutil import CN_TZ, next_daily_at

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
        *,
        now: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.state = state
        self.actions = actions
        self.now = now or time.time
        self.sleep = sleep or time.sleep
        sched = cfg.get("schedule", {}) or {}
        self.day_boundary = sched.get("day_boundary", "00:00")
        self.hourly = float(sched.get("hourly_interval_sec", 3600))
        self.fallback = float(cfg["travel"].get("fallback_interval_sec", 3600))

    # ---- 排程辅助 -----------------------------------------------------------
    def _next_boundary(self) -> float:
        """下一个北京时间 00:00（= 当天 23:59 之后）。"""
        now_dt = datetime.fromtimestamp(self.now(), CN_TZ)
        return next_daily_at(self.day_boundary, now_dt).timestamp()

    def _retry(self) -> float:
        """未得到确定结论：从当前时刻向后滚动一个轮询间隔。"""
        return self.now() + self.hourly

    def _schedule(self, name: str, due: float) -> Job:
        self.state.kv_set(f"next_{name}_at", due)
        return Job(due, name, getattr(self, f"_{name}"))

    # ---- 线 1：签到 ---------------------------------------------------------
    def _checkin(self, _job: Job) -> Job | None:
        if not self.cfg["daily_checkin"]["enabled"]:
            return self._schedule("checkin", self._next_boundary())
        result = self.actions.daily_checkin()
        confirmed = bool(result.ok and result.data.get("confirmed"))
        # 确认成功 -> 禁用到当天 23:59；否则每小时继续轮询
        due = self._next_boundary() if confirmed else self._retry()
        logger.info("checkin line: confirmed=%s next=%s", confirmed, due)
        return self._schedule("checkin", due)

    # ---- 线 2：兑换检查 -----------------------------------------------------
    def _exchange(self, _job: Job) -> Job | None:
        result = self.actions.check_exchanges()
        # 兑换成功或确认无可兑换 -> 禁用到当天 23:59；失败则每小时重试
        due = self._next_boundary() if result.ok else self._retry()
        logger.info("exchange line: ok=%s next=%s", result.ok, due)
        return self._schedule("exchange", due)

    # ---- 线 3：派遣 / 领礼物（动态滚动） ------------------------------------
    def _travel(self, _job: Job) -> Job | None:
        self.actions.travel_tick(force=True)
        due = self.state.kv_get_float("travel_due_at")
        if due is None or due <= self.now():
            due = self.now() + self.fallback
        logger.info("travel line: next=%s (real countdown)", due)
        return self._schedule("travel", due)

    def initial_jobs(self, now_ts: float | None = None) -> list[Job]:
        """重启/启动后三条线立即各跑一次，之后由各线自行滚动。"""
        now = now_ts if now_ts is not None else self.now()
        jobs: list[Job] = []
        if self.cfg["daily_checkin"]["enabled"]:
            jobs.append(Job(now, "checkin", self._checkin))
        growth = self.cfg.get("growth", {}) or {}
        if growth.get("enabled") and growth.get("auto_exchange", True):
            jobs.append(Job(now, "exchange", self._exchange))
        if self.cfg["travel"]["enabled"]:
            jobs.append(Job(now, "travel", self._travel))
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
            except Exception as exc:  # 单条线失败不能拖垮整个进程
                logger.exception("job %s failed: %s", job.name, exc)
                self.state.log(f"scheduler.{job.name}", False, str(exc))
                next_job = Job(self.now() + 300, job.name, job.fn)
            executed += 1
            if next_job is not None:
                heapq.heappush(heap, next_job)
        return executed
