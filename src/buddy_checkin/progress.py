"""当日任务进度账本（接续文件）：每个任务今天做到哪一步、证据是什么。

为什么需要它：客户端侧「今天还能不能做」的状态经常是推测（卡片在不在、
按钮 disabled 与否），一旦某轮读到错的信号，就可能把「没做」记成「做完了」，
当天再也不做 —— 2026-11-13 漏签就是这么来的（00:00:51 把前一天的 streak 当成
今天的）。所以要有一份显式账本：

    pending      还没成功，继续重试
    done         已用**可复核的证据**确认完成（服务端状态 / DOM 消失 / 倒计时归零）
    impossible   今天确实做不了了（次数用尽 / 补登卡为 0 / 没有抽奖券）

只有所有任务都进入 done / impossible，才算「今天 100% 完成、无法再继续」，
才允许静默到 23:59。次日（北京时间 0 点）账本自动翻页，全部重置为 pending。

**重启不信任账本**：进程启动时无论账本写着什么，都要用真实状态重新核对一遍
（服务端 today_checked_in、日历剩余漏签、兑换结论、抽奖券数、派遣状态），
把账本改回真实值。这样即使上一进程写错了状态，重启也能立即纠正，而不是
跟着错到底。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .timeutil import day_key

PENDING = "pending"
DONE = "done"
IMPOSSIBLE = "impossible"

# 与调度器的任务顺序一致：先签到、补签，再兑换，最后抽奖/派遣
TASK_ORDER = ("checkin", "makeup", "exchange", "lottery", "travel")

TASK_LABELS = {
    "checkin": "每日签到",
    "makeup": "漏签补签",
    "exchange": "兑换奖励",
    "lottery": "抽奖",
    "travel": "派遣/领奖",
}


@dataclass
class TaskRecord:
    state: str = PENDING
    detail: str = ""
    attempts: int = 0
    updated_at: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.state in (DONE, IMPOSSIBLE)


class Progress:
    """当日任务账本，落盘为单个 JSON（原子写，避免半个文件）。"""

    def __init__(self, path: str | Path, day: str | None = None) -> None:
        self.path = Path(str(path)).expanduser()
        self.day = day or day_key()
        self.tasks: dict[str, TaskRecord] = {}
        self.silent_until: float | None = None
        self.generation: int = 0
        self.loaded_from_disk: bool = False
        self.rollovers: int = 0
        self._load()

    # ---- 读写 ---------------------------------------------------------------
    def _load(self) -> None:
        for name in TASK_ORDER:
            self.tasks[name] = TaskRecord()
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 账本坏了就当没有：宁可重跑一遍，也不按坏数据静默
            return
        self.loaded_from_disk = True
        stored_day = str(raw.get("day") or "")
        if stored_day != self.day:
            # 跨天：账本作废，一切从 pending 开始（silent_until 一并清掉）
            self.rollovers += 1
            self._save()
            return
        for name, rec in (raw.get("tasks") or {}).items():
            if name not in self.tasks:
                continue
            self.tasks[name] = TaskRecord(
                state=rec.get("state", PENDING),
                detail=str(rec.get("detail") or ""),
                attempts=int(rec.get("attempts") or 0),
                updated_at=float(rec.get("updated_at") or 0),
                evidence=dict(rec.get("evidence") or {}),
            )
        self.silent_until = raw.get("silent_until")
        self.generation = int(raw.get("generation") or 0)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "day": self.day,
            "silent_until": self.silent_until,
            "generation": self.generation,
            "updated_at": time.time(),
            "tasks": {n: asdict(r) for n, r in self.tasks.items()},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---- 查询 ---------------------------------------------------------------
    def get(self, name: str) -> TaskRecord:
        return self.tasks.setdefault(name, TaskRecord())

    def is_terminal(self, name: str) -> bool:
        return self.get(name).terminal

    def all_terminal(self) -> bool:
        return all(r.terminal for r in self.tasks.values())

    def summary(self) -> dict[str, dict[str, Any]]:
        return {n: asdict(r) for n, r in self.tasks.items()}

    def pending_tasks(self) -> list[str]:
        return [n for n in TASK_ORDER if not self.is_terminal(n)]

    # ---- 写入 ---------------------------------------------------------------
    def mark(self, name: str, state: str, detail: str = "",
             evidence: dict[str, Any] | None = None) -> TaskRecord:
        rec = self.get(name)
        rec.state = state
        rec.detail = detail[:600]
        rec.updated_at = time.time()
        rec.evidence = evidence or {}
        if state == PENDING:
            rec.attempts += 1
        self._save()
        return rec

    def reset(self, name: str | None = None) -> None:
        names = [name] if name else list(self.tasks)
        for n in names:
            self.tasks[n] = TaskRecord()
        self._save()

    def mark_silent(self, until: float | None) -> None:
        self.silent_until = until
        self._save()

    def bump_generation(self) -> int:
        self.generation += 1
        self._save()
        return self.generation

    def reset_all(self, day: str | None = None) -> None:
        """跨天/强制重来：清空所有任务与静默标记。"""
        self.day = day or day_key()
        self.tasks = {n: TaskRecord() for n in TASK_ORDER}
        self.silent_until = None
        self._save()


def default_path(cfg: dict) -> Path:
    base = Path(str(cfg.get("state_db", "~/.local/share/buddy-web-checkin/state.sqlite3"))
                ).expanduser()
    return base.parent / "progress.json"
