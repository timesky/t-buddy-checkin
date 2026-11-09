"""零第三方依赖的配置层（tomllib + 标准库）。

架构：两条互不干扰的执行通道
  - web     : 网页端，agent-browser headless 独立 session/profile
  - desktop : 桌面端，仅用于每日签到，优先 agent-desktop 无障碍树

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
    # 网页端：独立 headless 会话，绝不接管用户正在用的浏览器
    "web": {
        "engine": "agent-browser",
        "bin": "agent-browser",
        "session": "buddy-checkin",
        "profile": "~/.local/share/buddy-web-checkin/browser-profile",
        # 复用 Edge 登录态时必须用 Edge 自己的可执行文件解密 v10 cookie；
        # 留空则用 agent-browser 自带 Chrome（扫码路线）。
        "executable_path": "",
        "edge_profile_source": "~/Library/Application Support/Microsoft Edge",
        "headless": True,
        "timeout_sec": 30,
        "login_wait_sec": 180,
    },
    # 桌面端：只做每日签到。engine = auto | agent-desktop | cdp | computer-use
    "desktop": {
        # cua-driver 走已授权的 com.trycua.driver AX 路径，实测可点签到按钮
        "engine": "auto",
        "bin": "agent-desktop",
        "cua_driver_bin": "/Applications/CuaDriver.app/Contents/MacOS/cua-driver",
        "app": "WorkBuddy",
        "cdp_port": 9222,
        "allow_focus": False,  # 永远不主动激活窗口 / 不移动鼠标
    },
    "daily_checkin": {"enabled": True, "at": "09:00", "catch_up": True},
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
    },
    # 成长中心：CuaDriver AX 通道操作已登录的 Edge「个人中心」页面
    "growth": {
        "enabled": True,
        "cua_driver_bin": "/Applications/CuaDriver.app/Contents/MacOS/cua-driver",
        "browser_app": "Microsoft Edge",
        "title_hint": "个人中心",
        "scene": "",              # 留空用页面默认场景
        "claim_offset_sec": 3,    # 倒计时归零后再等 3 秒，避免服务端未结算
        "auto_exchange": True,    # 自动兑换连登奖励/抽奖/满能量盲盒；补登卡永不自动消耗
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


def profile_path(cfg: dict) -> Path:
    path = expand(cfg["web"]["profile"])
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
