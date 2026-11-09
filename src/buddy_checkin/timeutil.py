"""时间与到期解析。

口径铁律：所有入库与展示时间统一按北京时间（Asia/Shanghai）。
UTC 源 / 带 Z 的 ISO 正常转；naive 字符串一律按北京时间解释，禁止原样当 UTC。
内部调度使用 epoch 秒（绝对时间），不受本地时区设置影响。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")

_NUM = re.compile(r"^-?\d+(?:\.\d+)?$")
_CLOCK = re.compile(r"^(?:(\d+):)?(\d+):(\d+)$")


def beijing_now() -> datetime:
    return datetime.now(CN_TZ)


def day_key(ts: float | None = None) -> str:
    """按北京时间取日期，用于幂等键。"""
    dt = datetime.fromtimestamp(ts, CN_TZ) if ts is not None else beijing_now()
    return dt.strftime("%Y-%m-%d")


def format_ts(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, CN_TZ).strftime("%Y-%m-%d %H:%M:%S %z")


def parse_timestamp(value: Any) -> float | None:
    """服务端时间字段 -> epoch 秒。支持 epoch 秒/毫秒、ISO、常见日期串。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v <= 0:
            return None
        return v / 1000.0 if v > 1e11 else v
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if _NUM.match(s):
            return parse_timestamp(float(s))
        dt: datetime | None = None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CN_TZ)  # naive 按北京时间，绝不按 UTC
        return dt.timestamp()
    return None


def parse_countdown(value: Any) -> float | None:
    """倒计时 -> 剩余秒数。支持数字、HH:MM:SS、MM:SS。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    if isinstance(value, str):
        s = value.strip()
        if _NUM.match(s):
            return max(0.0, float(s))
        m = _CLOCK.match(s)
        if m:
            hours = int(m.group(1) or 0)
            return float(hours * 3600 + int(m.group(2)) * 60 + int(m.group(3)))
    return None


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def find_field(obj: Any, names: list[str] | tuple[str, ...], max_depth: int = 8) -> Any:
    """在嵌套 JSON 中按候选字段名找第一个标量值。"""
    targets = {_norm(n) for n in names}
    stack: list[tuple[Any, int]] = [(obj, 0)]
    while stack:
        cur, depth = stack.pop()
        if depth > max_depth:
            continue
        if isinstance(cur, dict):
            for key, val in cur.items():
                if _norm(key) in targets and not isinstance(val, (dict, list)):
                    return val
                if isinstance(val, (dict, list)):
                    stack.append((val, depth + 1))
        elif isinstance(cur, list):
            for item in cur[:50]:
                if isinstance(item, (dict, list)):
                    stack.append((item, depth + 1))
    return None


def travel_due(payload: Any, travel_cfg: dict, server_now: float | None = None) -> float | None:
    """从旅行状态响应算出绝对到期 epoch。优先绝对结束时间，其次剩余秒数。"""
    if not payload:
        return None
    base = server_now if server_now is not None else datetime.now().timestamp()
    end_raw = find_field(payload, travel_cfg.get("end_field_candidates", []))
    end_ts = parse_timestamp(end_raw)
    if end_ts is not None:
        return end_ts
    remain_raw = find_field(payload, travel_cfg.get("remaining_field_candidates", []))
    remain = parse_countdown(remain_raw)
    if remain is not None:
        return base + remain
    return None


def next_daily_at(hhmm: str, now: datetime | None = None) -> datetime:
    """下一个北京时间 HH:MM；已过则顺延到明天。"""
    now = now or beijing_now()
    hour, minute = (int(x) for x in hhmm.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def skew_from_http_date(date_header: str, local_ts: float) -> float:
    """用 HTTP Date 响应头算服务端时钟偏差（秒），用于校正到期时间。"""
    try:
        server_dt = parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return 0.0
    if server_dt is None:
        return 0.0
    if server_dt.tzinfo is None:
        server_dt = server_dt.replace(tzinfo=CN_TZ)
    return server_dt.timestamp() - local_ts
