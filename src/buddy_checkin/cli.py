"""命令行入口。所有命令默认后台/只读，不接管用户浏览器、不激活窗口。"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

from . import config
from .actions import Actions
from .adapters.desktop import CuaDriverSigner, DesktopSigner
from .adapters.growth import GrowthCenter
from .adapters.web import AgentBrowser
from .deps import cmd_deps
from .scheduler import Scheduler
from .state import State
from .timeutil import format_ts, next_daily_at

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EDGE = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
logger = logging.getLogger("buddy-checkin")

# 依赖 WorkBuddy：本技能运行在 WorkBuddy agent 内会消耗模型额度，必须运行在免费模型下。
# 脚本无法读取当前选中的模型，所以以环境变量 BUDDY_FREE_MODEL=1 作为运维侧显式确认。
FREE_MODEL_ENV = "BUDDY_FREE_MODEL"


def preflight_model_check(cfg: dict) -> int:
    """依赖 WorkBuddy，必须确认运行在免费模型下，否则拒绝执行。返回 0=放行，2=拒绝。"""
    rt = cfg.get("runtime", {}) or {}
    if not rt.get("require_free_model", False):
        return 0
    ack = os.environ.get(FREE_MODEL_ENV, "").strip().lower()
    if ack in ("1", "true", "yes", "on"):
        return 0
    hint = (rt.get("free_model_hint") or "WorkBuddy 模型选择器 → 免费模型")
    print(
        "✋ 本技能依赖 WorkBuddy，必须运行在「免费模型」下，否则会消耗付费额度。\n"
        f"   请先在 {hint} 选择免费模型，再执行本命令。\n"
        "   确认已选免费模型后，用环境变量显式确认并重试：\n"
        f"     {FREE_MODEL_ENV}=1 ./run.sh <command>\n"
        "   定时任务 / LaunchAgent 请将该变量写入启动环境（install-launchd 已内置）。",
        file=sys.stderr,
    )
    return 2


def cmd_model_check(cfg: dict, args: argparse.Namespace) -> int:
    """只读检查免费模型策略与当前确认状态，不拒绝执行。"""
    rt = cfg.get("runtime", {}) or {}
    require = bool(rt.get("require_free_model", False))
    ack = os.environ.get(FREE_MODEL_ENV, "").strip().lower() in ("1", "true", "yes", "on")
    print(f"require_free_model = {require}")
    print(f"free_model_hint   = {rt.get('free_model_hint', '-')}")
    print(f"{FREE_MODEL_ENV}        = {os.environ.get(FREE_MODEL_ENV, '<unset>')}")
    print(f"guard would pass  = {not require or ack}")
    return 0


def copy_edge_profile(src: Path, dst: Path) -> dict:
    """只读 Edge 源 profile，把登录态相关文件复制到独立副本（不动用户 Edge）。"""
    default_src, default_dst = src / "Default", dst / "Default"
    default_dst.mkdir(parents=True, exist_ok=True)
    dst.chmod(0o700)
    copied: list[str] = []
    cookie_src = default_src / "Cookies"
    if not cookie_src.exists():
        raise FileNotFoundError(f"Edge cookies not found: {cookie_src}")
    con_src = sqlite3.connect(f"file:{cookie_src}?mode=ro", uri=True)
    con_dst = sqlite3.connect(str(default_dst / "Cookies"))
    con_src.backup(con_dst)
    con_dst.close()
    con_src.close()
    copied.append("Cookies")
    for name in ("Preferences", "Secure Preferences"):
        path = default_src / name
        if path.exists():
            shutil.copy2(path, default_dst / name)
            copied.append(name)
    local_storage = default_src / "Local Storage"
    if local_storage.is_dir():
        shutil.copytree(local_storage, default_dst / "Local Storage", dirs_exist_ok=True)
        copied.append("Local Storage")
    local_state = src / "Local State"
    if local_state.exists():
        shutil.copy2(local_state, dst / "Local State")
        copied.append("Local State")
    return {"copied": copied, "source": str(src), "target": str(dst)}


def cmd_setup_from_edge(cfg: dict, args: argparse.Namespace) -> int:
    """复用 Edge 已登录状态：profile 副本 + Edge 可执行文件冷启动验证。"""
    src = config.expand(cfg["web"]["edge_profile_source"])
    dst = config.profile_path(cfg)
    exe = cfg["web"].get("executable_path") or DEFAULT_EDGE
    if not Path(exe).exists():
        print(f"[edge] browser executable not found: {exe}", file=sys.stderr)
        return 1
    try:
        report = copy_edge_profile(src, dst)
    except (OSError, sqlite3.Error) as exc:
        print(f"[edge] copy failed: {exc}", file=sys.stderr)
        return 1
    print(f"[edge] copied: {', '.join(report['copied'])} -> {report['target']}")

    effective = copy.deepcopy(cfg)
    effective["web"]["executable_path"] = exe
    _state, web, _desktop, _actions = build(effective)
    web.cold_restart()  # daemon 单例，不冷启动则 --executable-path 被忽略
    web.open()
    web.wait(5000)
    url = web.get_url()
    agent = web.eval_js("navigator.userAgent")
    web.close()
    logged_in = bool(url) and "/login" not in url
    print(f"[edge] userAgent: {agent[-140:]}")
    print(f"[edge] url      : {url}")
    if logged_in:
        print("[edge] OK: 登录态已复用，直接跑 ./run.sh once 即可。")
        return 0
    print(
        "[edge] 认证 cookie 未被接受（常见原因：Keychain 解密失败 / app-bound encryption）。\n"
        "[edge] 请在 macOS 终端（非沙箱）直接运行本命令；仍失败则改用 ./run.sh setup 扫码。",
        file=sys.stderr,
    )
    return 1


def build(cfg: dict):
    state = State(config.state_db_path(cfg))
    web = AgentBrowser(cfg)
    engine = cfg["desktop"].get("engine", "auto")
    if engine == "cua-driver":
        desktop = CuaDriverSigner(cfg)
    elif engine == "auto":
        # 优先走已授权的 CuaDriver AX 通道，不可用再回退
        desktop = CuaDriverSigner(cfg) if CuaDriverSigner(cfg).probe().ok else DesktopSigner(cfg)
    else:
        desktop = DesktopSigner(cfg)
    growth = GrowthCenter(cfg) if cfg.get("growth", {}).get("enabled") else None
    actions = Actions(cfg, state, web, desktop, growth=growth)
    return state, web, desktop, actions


def cmd_probe(cfg: dict, args: argparse.Namespace) -> int:
    _state, web, desktop, _actions = build(cfg)
    report = {"web": web.probe(), "desktop": desktop.probe().as_dict()}
    web.close()
    out = ROOT / "discovered.json"
    out.write_text(
        json.dumps(config.scrub(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[probe] wrote {out}")
    print(f"[probe] web url      : {report['web'].get('url', '-')}")
    print(f"[probe] desktop      : {report['desktop']['data'].get('chosen') or 'none'}")
    print(f"[probe] backends     : {report['desktop']['data'].get('backends')}")
    return 0


def cmd_setup(cfg: dict, args: argparse.Namespace) -> int:
    """headless 打开登录页，把二维码截图交给你扫码；全程不弹窗、不抢焦点。"""
    _state, web, _desktop, _actions = build(cfg)
    web.open()
    ready = web.wait_for_login_page()
    if not ready:
        print("[setup] 二维码未在 20 秒内渲染，仍尝试截图（可能只是骨架屏）。", file=sys.stderr)
    qr_path = str(ROOT / "login-qr.png")
    shot = web.screenshot(qr_path)
    wait_sec = int(cfg["web"].get("login_wait_sec", 180))
    print(f"[setup] 二维码截图: {shot}")
    print(f"[setup] 请用手机扫码登录；登录后本程序会自动继续（最多等待 {wait_sec // 60} 分钟）。")
    logged_in = False
    for _ in range(max(1, wait_sec // 3)):
        time.sleep(3)
        url = web.get_url()
        if url and "/login" not in url:
            logged_in = True
            break
    web.close()
    if not logged_in:
        print("[setup] 未检测到登录完成，登录态可能未保存，请重跑 setup。", file=sys.stderr)
        return 1
    print("[setup] 登录态已保存到独立 profile，日常浏览器不受影响。")
    return 0


def cmd_checkin_now(cfg: dict, args: argparse.Namespace) -> int:
    """只做每日签到，不碰网页端。供辅助功能授权后、或在终端直跑。"""
    state, _web, _desktop, actions = build(cfg)
    try:
        result = actions.daily_checkin()
        print(json.dumps(config.scrub(result.as_dict()), ensure_ascii=False, indent=2))
        return 0 if result.ok else 1
    finally:
        state.close()


def cmd_once(cfg: dict, args: argparse.Namespace) -> int:
    state, web, _desktop, actions = build(cfg)
    try:
        summary = actions.reconcile_all(reason="once")
        print(json.dumps(config.scrub(summary), ensure_ascii=False, indent=2))
        return 0 if summary["ok"] else 1
    finally:
        web.close()
        state.close()


def cmd_status(cfg: dict, args: argparse.Namespace) -> int:
    state, _web, desktop, _actions = build(cfg)
    due = state.kv_get_float("travel_due_at")
    print("== buddy-web-checkin status ==")
    print(f"今日(北京时间)      : {config.scrub(state.kv_get('last_checkin_day')) or '未签到'}")
    print(f"下次签到            : {format_ts(next_daily_at(cfg['daily_checkin']['at']).timestamp())}")
    print(f"旅行到期            : {format_ts(due)}")
    print(f"桌面端后端          : {desktop.probe().data.get('chosen') or 'none (需安装/授权)'}")
    print(f"网页端会话          : {cfg['web']['session']} (headless={cfg['web']['headless']})")
    print("-- 最近运行 --")
    for row in state.recent(10):
        print(f"  {format_ts(row['ts'])}  {'OK' if row['ok'] else 'FAIL'}  {row['action']}: {row['detail'][:100]}")
    state.close()
    return 0


def cmd_daemon(cfg: dict, args: argparse.Namespace) -> int:
    state, web, _desktop, actions = build(cfg)
    stop = threading.Event()
    scheduler = Scheduler(cfg, state, actions)

    def _stop(_signum, _frame):
        stop.set()

    try:
        import signal

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
    except (ImportError, ValueError):
        pass
    print("[daemon] started; waiting for next due (Ctrl-C to stop)")
    try:
        scheduler.run_forever(stop=stop)
    finally:
        web.close()
        state.close()
    return 0


def cmd_install_launchd(cfg: dict, args: argparse.Namespace) -> int:
    """只写 plist，不自动 load —— 加载动作留给你确认。"""
    script = ROOT / "run.sh"
    plist_path = Path.home() / "Library/LaunchAgents/com.local.buddy-web-checkin.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = config.expand("~/.local/share/buddy-web-checkin/daemon.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.local.buddy-web-checkin</string>
  <key>EnvironmentVariables</key>
  <dict>
    <!-- 确认已在 WorkBuddy 选「免费模型」后由本环境变量显式放行（见 runtime.require_free_model） -->
    <key>BUDDY_FREE_MODEL</key><string>1</string>
  </dict>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>{script}</string>
    <string>daemon</string>
  </array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log_path}</string>
  <key>StandardErrorPath</key><string>{log_path}</string>
</dict>
</plist>
"""
    plist_path.write_text(plist, encoding="utf-8")
    print(f"[install] wrote {plist_path}")
    print("[install] 未自动加载。确认后执行：")
    print(f"  launchctl bootstrap gui/$(id -u) {plist_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="buddy-checkin")
    parser.add_argument("--config", help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe", help="read-only probe of web + desktop backends")
    sub.add_parser("setup", help="headless QR login for the isolated web profile")
    sub.add_parser("setup-from-edge", help="reuse an already logged-in Edge profile (no QR scan)")
    sub.add_parser("checkin-now", help="daily checkin only (desktop lane), no web actions")
    sub.add_parser("once", help="run one full reconcile pass")
    sub.add_parser("status", help="read-only status")
    sub.add_parser("daemon", help="run the exact-expiry scheduler")
    sub.add_parser("install-launchd", help="write LaunchAgent plist (does not load it)")
    sub.add_parser("model-check", help="show free-model policy and current acknowledgment state")
    # 第三方依赖：安装技能时必须先 install 再 check，确认外部 CLI/GUI 就绪
    dep = sub.add_parser("deps", help="第三方依赖检查/安装引导（安装技能时必跑）")
    dep_sub = dep.add_subparsers(dest="action", required=True)
    dep_sub.add_parser("check", help="检查所有第三方依赖是否就绪（默认）")
    dep_sub.add_parser("install", help="安装可经 npm 管理的依赖，并给出 GUI 依赖手动指引")
    dep_sub.add_parser("list", help="仅列出依赖与路径")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cfg = config.load(args.config)
    logging.getLogger().setLevel(getattr(logging, cfg.get("log_level", "INFO"), logging.INFO))
    # 依赖 WorkBuddy：执行前必须确认运行在免费模型下（model-check / deps 属本地运维，
    # 不消耗模型额度，放行）
    if args.command not in ("model-check", "deps"):
        rc = preflight_model_check(cfg)
        if rc:
            return rc
    handlers = {
        "probe": cmd_probe,
        "setup": cmd_setup,
        "setup-from-edge": cmd_setup_from_edge,
        "checkin-now": cmd_checkin_now,
        "once": cmd_once,
        "status": cmd_status,
        "daemon": cmd_daemon,
        "install-launchd": cmd_install_launchd,
        "model-check": cmd_model_check,
        "deps": cmd_deps,
    }
    return handlers[args.command](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
