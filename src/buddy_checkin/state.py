"""SQLite 状态层：幂等领取、KV、运行日志。仅用标准库。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .timeutil import day_key


class State:
    def __init__(self, db_path: str | Path):
        self.conn = sqlite3.connect(str(db_path), timeout=10)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._init_tables()

    def _init_tables(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS kv (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS claims (
                day     TEXT NOT NULL,
                kind    TEXT NOT NULL,
                item_id TEXT NOT NULL,
                ts      REAL NOT NULL,
                PRIMARY KEY (day, kind, item_id)
            );
            CREATE TABLE IF NOT EXISTS runs (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                ts     REAL NOT NULL,
                action TEXT NOT NULL,
                ok     INTEGER NOT NULL,
                detail TEXT
            );
            """
        )
        self.conn.commit()

    # ---- kv -----------------------------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            return row[0]

    def kv_set(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        self.conn.execute(
            "INSERT INTO kv(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, payload),
        )
        self.conn.commit()

    def kv_get_float(self, key: str, default: float | None = None) -> float | None:
        value = self.kv_get(key)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    # ---- 幂等 ---------------------------------------------------------------
    def claim_once(self, kind: str, item_id: str, ts: float | None = None) -> bool:
        """同一北京时间自然日内、同一类型、同一项目只允许领一次。"""
        import time

        stamp = ts if ts is not None else time.time()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO claims(day, kind, item_id, ts) VALUES (?, ?, ?, ?)",
            (day_key(stamp), kind, item_id, stamp),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def already_claimed(self, kind: str, item_id: str, ts: float | None = None) -> bool:
        import time

        stamp = ts if ts is not None else time.time()
        row = self.conn.execute(
            "SELECT 1 FROM claims WHERE day = ? AND kind = ? AND item_id = ?",
            (day_key(stamp), kind, item_id),
        ).fetchone()
        return row is not None

    # ---- 运行日志 -----------------------------------------------------------
    def log(self, action: str, ok: bool, detail: str = "") -> None:
        import time

        self.conn.execute(
            "INSERT INTO runs(ts, action, ok, detail) VALUES (?, ?, ?, ?)",
            (time.time(), action, 1 if ok else 0, detail[:1000]),
        )
        self.conn.commit()

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT ts, action, ok, detail FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            {"ts": r[0], "action": r[1], "ok": bool(r[2]), "detail": r[3]} for r in rows
        ]

    def close(self) -> None:
        self.conn.close()
