"""零第三方依赖的配置层（tomllib + 标准库）。

唯一外部依赖是 **CuaDriver**（trycua 出品的 AX 驱动）：签到走桌面客户端窗口，
成长中心走已登录的个人中心浏览器窗口，二者都经 CuaDriver。不到其它外部 CLI
（agent-browser / agent-desktop / cdp / computer-use 均未使用）。

完整可配置项见仓库根目录 config.example.toml。
"""
from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "base_url": "https://www.workbuddy.cn/app",
    "state_db": "~/.local/share/buddy-web-checkin/state.sqlite3",
    # 桌面端：只做每日签到。唯一后端是 CuaDriver（cua-driver 走已授权的
    # com.trycua.driver AX 路径，实测可点签到按钮，不移动光标、不抢焦点）。
    "desktop": {
        "cua_driver_bin": "/Applications/CuaDriver.app/Contents/MacOS/cua-driver",
        "app": "WorkBuddy",
        # 面板收起时用于展开的入口按钮；不同客户端版本文案可能不同
        "station_entry_labels": ["Buddy加油站", "做任务赢积分"],
        # 点击后出现的「今天已领」文案，作为已签到的强证据（只在面板区内认）
        "already_claimed_labels": ["今日已领取", "已领取", "明日再来", "今日已领"],
        # 加油站面板索引窗口：入口 idx + 该值 = 面板上界，超出即对话正文，不参与已签判定
        "station_panel_window": 16,
    },
    # 签到时间线由 [schedule] 控制（0 点恢复 / 每小时轮询），此处只留总开关
    # min_interval_sec：两次真实签到的最小间隔（默认 24h，满足「满 24 小时」口径）
    "daily_checkin": {"enabled": True, "min_interval_sec": 86400},
    # 统一进程内的三条调度线（仅 travel 为动态滚动）：
    #   1 checkin  每天 0 点恢复每小时轮询，签到成功即禁用到当天 23:59
    #   2 exchange 每天 0 点恢复每小时轮询，兑换成功或确认无可兑换即禁用到当天 23:59
    #   3 travel   重启先领礼物/派遣，之后按真实倒计时动态滚动
    # 三条线在进程启动时都立即执行一次，再各自从该时刻向后滚动。
    "schedule": {
        "day_boundary": "00:00",
        "hourly_interval_sec": 3600,
    },
    "gifts": {
        "enabled": True,
        "allow_keywords": ["今日礼包", "签到", "连签", "盲盒", "抽奖", "旅行", "奖励", "领取"],
        "deny_keywords": ["去完成", "邀约", "充值", "购买", "开通", "升级"],
        "max_claims_per_run": 20,
    },
    "travel": {
        "enabled": True,
        "end_field_candidates": [
            "end_at", "endTime", "end_time", "finish_at", "finishTime",
            "expire_at", "travel_end_at", "return_at", "arrive_at",
        ],
        "remaining_field_candidates": [
            "remaining_seconds", "remainSeconds", "remain_seconds", "left_seconds",
            "countdown", "remaining", "leftTime", "seconds_left",
        ],
        "state_field_candidates": ["state", "status", "travel_state"],
        "state_values": {
            "idle": ["idle", "none", "not_started", "ready"],
            "traveling": ["traveling", "travelling", "running", "in_progress", "ongoing"],
            "claimable": ["claimable", "completed", "finished", "done", "reward_pending"],
        },
        "claim_offset_sec": 3,
        "re_dispatch": True,
        "fallback_interval_sec": 3600,
        # 派遣次数当天用尽时禁用到此时刻（北京时间）；已过则次日 0 点再试
        "exhausted_retry_at": "23:00",
    },
    # 成长中心：CuaDriver AX 通道操作已登录的成长中心页面（任意浏览器，不限定 Edge）
    "growth": {
        "enabled": True,
        "cua_driver_bin": "/Applications/CuaDriver.app/Contents/MacOS/cua-driver",
        # auto = 探测任意浏览器里已打开的成长中心窗口；如需限定某浏览器可填其 app 名
        "browser_app": "auto",
        "title_hint": "个人中心",
        "scene": "",              # 留空用页面默认场景
        "claim_offset_sec": 3,    # 倒计时归零后再等 3 秒，避免服务端未结算
        "auto_exchange": True,    # 自动兑换连登奖励/抽奖/满能量盲盒；补登卡永不自动消耗
        # 浏览器里没有成长中心页面时，自动从 WorkBuddy 客户端入口触发打开。
        # 不同客户端版本入口文案不同，可按需增补；带冷却避免反复打开新标签页。
        "auto_open_page": True,
        # 「成长计划」实测会打开 CodeBuddy 积分页，故不放在默认候选里
        "auto_open_labels": ["做任务赢积分", "任务中心", "Buddy加油站"],
        "auto_open_wait_sec": 6,
        "auto_open_cooldown_sec": 300,
        # 点击入口后仍找不到页面时的兜底：后台打开目标 URL（-g 不抢焦点）
        "auto_open_url": "https://www.workbuddy.cn/profile/growth-center",
        "auto_open_url_fallback": True,
        # web 动作前先刷新页面（页面久置会保留旧状态），纯 AX 点工具栏刷新
        "refresh_before_action": True,
        "refresh_wait_sec": 3,
    },
    # DOM 文本兜底：接口未知或改版时按可见文字定位
    "dom_fallback": {
        "checkin": ["立即领取", "领取今日礼包", "今日礼包", "签到领取"],
        "dispatch": ["派猫猫旅行", "派 Buddy 去旅行", "派出去", "出发采风"],
        "claim_travel": ["领取旅行奖励", "领取奖励", "收下礼物", "领取礼物"],
        "gift_claim": ["领取", "开盲盒", "开启一次盲盒", "抽奖", "立即抽奖"],
        "already_claimed": ["已领取", "明日再来", "今日已领", "已签到"],
        "traveling": ["旅行中", "采风中", "倒计时"],
    },
    "notify": {"webhook": "", "macos": False},
    "log_level": "INFO",
}

# 任何含这些片段的键都不写入 discovered.json，也不进日志
SENSITIVE_KEYS = (
    "token", "secret", "cookie", "authorization", "password", "credential",
    "session", "jwt", "refresh", "access_key", "private_key", "sign",
)


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def expand(path: str | Path) -> Path:
    return Path(str(path)).expanduser().resolve()


# 从任意目录调用 run.sh 都必须读到项目根的配置，而不是调用方的 cwd
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.toml"


def load(path: str | Path | None = None) -> dict[str, Any]:
    """未指定路径时读项目根 config.toml；不存在则使用纯默认配置。"""
    cfg_path = expand(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        return copy.deepcopy(DEFAULTS)
    with cfg_path.open("rb") as fh:
        user_cfg = tomllib.load(fh) or {}
    return _merge(DEFAULTS, user_cfg)


def state_db_path(cfg: dict) -> Path:
    path = expand(cfg["state_db"])
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def scrub(obj: Any) -> Any:
    """递归脱敏，避免任何凭据落到磁盘或日志。"""
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if any(s in str(k).lower() for s in SENSITIVE_KEYS) else scrub(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [scrub(v) for v in obj[:50]]
    if isinstance(obj, str) and len(obj) > 300:
        return obj[:300] + "..."
    return obj
