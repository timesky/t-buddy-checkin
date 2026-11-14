"""零第三方依赖的配置层（tomllib + 标准库）。

运行时依赖：**agent-browser（CDP）**。通过 WorkBuddy 客户端的调试端口
（WORKBUDDY_REMOTE_DEBUGGING_PORT）连接内嵌的成长中心 iframe，DOM 级完成
签到状态校验、礼物/盲盒、派遣、兑换、抽奖。

旧的 CuaDriver / AX 通道已停用（desktop.engine / growth.engine="ax" 仅为
回退排查保留），agent-desktop 作为机器上可选的 AX 兜底工具。

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
    # 桌面端：签到走左下角「Buddy加油站」浮层（FuelCard），不经桌面端。
    # 本段只保留 agent-desktop 作为 CDP 不可达时的人工排查兜底（不参与日常流程）。
    "desktop": {
        "app": "WorkBuddy",
        # agent-desktop 引擎（严格 headless，失败绝不降级 headed）
        "agent_desktop_bin": "node_modules/.bin/agent-desktop",  # 相对项目 tools/ 或 PATH
        "agent_desktop_session": "buddy-checkin",
        # 检测到登录/验证码：0=只截图+报告不阻塞；>0=等待人工登录的秒数
        "login_wait_sec": 0,
        "screenshot_dir": "~/.local/share/buddy-web-checkin/screenshots",
    },
    # 签到时间线由 [schedule] 控制（0 点恢复 / 每小时轮询），此处只留总开关。
    # 签到 = 点浮层「立即领取」；确认领到才落库，无 24h 冷却。
    "daily_checkin": {"enabled": True},
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
    # 成长中心：CDP 通道（agent-browser 打开入口 + 直连 iframe 做 DOM 操作）
    "growth": {
        "enabled": True,
        # browser(默认, CDP) | ax(历史 CuaDriver 通道，仅回退排查时显式启用)
        "engine": "browser",
        # WorkBuddy 客户端调试端口：需 WORKBUDDY_REMOTE_DEBUGGING_PORT 环境变量
        # 与客户端重启配合（见 scripts/launch-workbuddy-debug.sh）
        "cdp_port": 9222,
        # agent-browser 可执行文件；留空按 tools/node_modules/.bin → PATH 顺序探测
        "agent_browser_bin": "",
        # 从客户端侧栏打开成长中心的入口文案（依次尝试）
        "open_labels": ["成长计划", "做任务赢积分", "任务中心"],
        # 点开入口后等待 iframe 出现的秒数上限
        "open_wait_sec": 6,
        "scene": "",              # 留空用页面默认场景
        "claim_offset_sec": 3,    # 倒计时归零后再等 3 秒，避免服务端未结算
        "auto_exchange": True,    # 自动兑换连登奖励/抽奖/满能量盲盒；补登卡永不自动消耗
        # 任务结束（领取/派遣/兑换/签到校验）后关闭成长中心弹窗。
        # 只关「本次由工具打开」的弹窗；用户自己开着的页面保持不动，不抢界面。
        "close_page_after_action": True,
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
