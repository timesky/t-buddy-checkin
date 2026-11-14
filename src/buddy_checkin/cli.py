"""命令行入口。所有命令默认后台/只读，不接管用户浏览器、不激活窗口。"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from pathlib import Path

from . import config
from .actions import Actions
from .adapters.fuel_card import FuelCard
from .adapters.growth_browser import GrowthBrowser
from .deps import cmd_deps
from .scheduler import Scheduler
from .state import State
from .timeutil import format_ts

ROOT = Path(__file__).resolve().parents[2]
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


def build(cfg: dict):
    """构造后端组合：全部走 CDP（agent-browser + 客户端调试端口）。

    - 签到        : 点左下角「Buddy加油站」浮层（FuelCard）
    - 礼物/派遣/兑换 : GrowthBrowser 直连成长中心 iframe 做 DOM 级操作

    旧 CuaDriver/AX 通道（desktop.CuaDriverSigner、growth.GrowthCenter）
    已不再装配；[growth].engine="ax" 只是为回退排查保留的显式开关。
    """
    state = State(config.state_db_path(cfg))
    growth = _build_growth(cfg, state)
    fuel = _build_fuel(cfg)
    # 签到不再依赖桌面端点击，desktop 传 None
    actions = Actions(cfg, state, None, growth=growth, fuel=fuel)
    return state, None, actions


def _build_fuel(cfg: dict):
    """签到通道：主窗口左下角浮层。CDP 不可达时由 FuelCard 明确报错。"""
    g = cfg.get("growth", {}) or {}
    if not g.get("enabled"):
        return None
    return FuelCard(cfg)


def _build_growth(cfg: dict, state):
    """构造成长中心适配器：唯一实现是 GrowthBrowser（CDP）。

    旧 CuaDriver/AX 通道已移除，这里没有回退分支——CDP 不可达时由
    GrowthBrowser 返回明确的 blocked 结果，而不是静默换通道。
    """
    g = cfg.get("growth", {}) or {}
    if not g.get("enabled"):
        return None
    return GrowthBrowser(cfg, state=state)


def _json(result) -> int:
    print(json.dumps(config.scrub(result.as_dict()), ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


def cmd_checkin_now(cfg: dict, args: argparse.Namespace) -> int:
    """手动：只做签到时间线的一轮（不碰成长中心）。"""
    state, _desktop, actions = build(cfg)
    try:
        return _json(actions.daily_checkin())
    finally:
        state.close()


def cmd_exchange_now(cfg: dict, args: argparse.Namespace) -> int:
    """手动：只做兑换检查时间线的一轮（不碰签到、不碰派遣）。"""
    state, _desktop, actions = build(cfg)
    try:
        return _json(actions.check_exchanges())
    finally:
        state.close()


def cmd_travel_now(cfg: dict, args: argparse.Namespace) -> int:
    """手动：只做旅行时间线的一轮（领礼物 -> 派遣 -> 读真实倒计时）。"""
    state, _desktop, actions = build(cfg)
    try:
        return _json(actions.travel_tick(force=True))
    finally:
        state.close()


def cmd_once(cfg: dict, args: argparse.Namespace) -> int:
    state, _desktop, actions = build(cfg)
    try:
        summary = actions.reconcile_all(reason="once")
        print(json.dumps(config.scrub(summary), ensure_ascii=False, indent=2))
        return 0 if summary["ok"] else 1
    finally:
        state.close()


def cmd_status(cfg: dict, args: argparse.Namespace) -> int:
    state, _desktop, _actions = build(cfg)
    print("== buddy-web-checkin status ==")
    print(f"今日(北京时间)      : {config.scrub(state.kv_get('last_checkin_day')) or '未签到'}")
    g = cfg.get("growth", {}) or {}
    engine = (g.get("engine") or "browser").strip().lower()
    port = g.get("cdp_port", 9222)
    print(f"成长中心引擎        : {engine} (cdp_port={port})")
    if engine == "browser":
        gb = GrowthBrowser(cfg)
        alive = gb.available()
        page = "已打开" if alive and gb._iframe_ws() else ("未打开" if alive else "-")
        print(f"CDP {port:<16}: {'可达' if alive else '不可达（客户端未带调试端口启动）'}  页面: {page}")
    print("-- 三条时间线（统一常驻进程）--")
    for name, label in (("checkin", "签到检查"), ("exchange", "兑换检查"), ("travel", "旅行/派遣")):
        due = state.kv_get_float(f"next_{name}_at")
        shown = format_ts(due) if due else "首轮执行中（三条线顺序启动，稍后写入）"
        print(f"  {label:10} 下次: {shown}")
    print("-- 最近运行 --")
    for row in state.recent(10):
        print(f"  {format_ts(row['ts'])}  {'OK' if row['ok'] else 'FAIL'}  {row['action']}: {row['detail'][:100]}")
    state.close()
    return 0


def cmd_daemon(cfg: dict, args: argparse.Namespace) -> int:
    state, _desktop, actions = build(cfg)
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
    print("[daemon] started: 三条时间线(checkin/exchange/travel)均已立即执行一轮；"
          " 签到/兑换按日周期，旅行按真实倒计时滚动 (Ctrl-C 退出)")
    try:
        scheduler.run_forever(stop=stop)
    finally:
        state.close()
    return 0


def cmd_inspect(cfg: dict, args: argparse.Namespace) -> int:
    """只读诊断：打印 CDP 连通性、targets、成长中心页面状态与签到判定。

    不点击、不改状态。另一台机器签到/派遣失败时先跑它，把输出贴回来即可判断
    是客户端没带调试端口、页面没打开、还是签到未记录。
    """
    g = cfg.get("growth", {}) or {}
    port = g.get("cdp_port", 9222)
    gb = GrowthBrowser(cfg)
    print("== CDP 诊断 ==")
    print(f"  port            : {port}")
    print(f"  agent-browser   : {gb.ab_bin or '未找到（tools/node_modules/.bin/agent-browser）'}")
    print(f"  node            : {gb.node_bin or '未找到'}")
    print(f"  cdp_client.mjs  : {gb.cdp_js} {'OK' if gb.cdp_js.exists() else 'MISSING'}")
    targets = gb.targets()
    print(f"  targets         : {len(targets)} 个")
    if targets:
        owned, who = gb.port_owner(targets)
        print(f"  端口归属        : {'WorkBuddy ✓' if owned else '不是 WorkBuddy ✗'}  {who}")
    for t in targets:
        print(f"    [{t.get('type'):<8}] {(t.get('title') or '')[:34]:<36} {(t.get('url') or '')[:60]}")
    if not targets:
        code, msg = gb.block or ("cdp_unavailable", "unknown")
        print(f"  ✗ CDP 不可达（{code}）：{msg}")
        if code == "cdp_port_foreign":
            print("    该端口被其它浏览器/Electron 应用占用，换成 WorkBuddy 的调试端口再试：")
            print(f"    launchctl setenv WORKBUDDY_REMOTE_DEBUGGING_PORT {port} 并重启客户端")
            return 1
        print("    修复：launchctl setenv WORKBUDDY_REMOTE_DEBUGGING_PORT "
              f"{port} && 重启 WorkBuddy 客户端")
        return 1

    fc = FuelCard(cfg)
    fst = fc.probe()
    print("\n== 签到状态（左下角 Buddy加油站 浮层）==")
    if not fst.get("ok"):
        print(f"  读取失败: {fst.get('detail')}")
    else:
        card = fst.get("card") or {}
        print(f"  浮层卡片    : {card.get('title') or (fst.get('detail') or '')[:40]}")
        print(f"  副标题      : {card.get('sub') or '-'}")
        print(f"  按钮        : {card.get('btn') or '-'}"
              f"（type={(card.get('props') or {}).get('type')}）")
        print(f"  轮播        : " + ", ".join(
            f"{d.get('label')}{'*' if d.get('on') else ''}" for d in card.get("dots") or []))
        verdict = ("今日可领（未签）" if fst.get("present")
                   else "今日已领或浮层无签到项" if fst.get("claimed")
                   else "状态未知")
        print(f"  判定        : {verdict}")

    ws = gb._iframe_ws()
    print(f"  成长中心 iframe : {'已打开' if ws else '未打开'}")
    if not ws:
        print("    （运行 exchange-now / travel-now 会自动从客户端入口打开）")
        return 0

    st = gb.checkin_status()
    print("\n== 成长中心活跃地图（仅参考，不是签到判据）==")
    if st.get("ok"):
        print(f"  今日已打卡  : {st.get('checked')}")
        print(f"  连续登录    : {st.get('streak')} 天")
        print(f"  今日格子底色: {st.get('bg')}")
        if st.get("makeup"):
            print(f"  漏签可补    : {','.join(st['makeup'])}（补签卡不自动消耗）")
    else:
        print(f"  读取失败: {st.get('detail')}")

    page = gb.read_state()
    print("\n== 页面状态 ==")
    print(f"  state       : {page['state']}")
    print(f"  remaining   : {page['remaining_sec']}s")
    print("  buttons     :")
    for b in (page.get("buttons") or [])[:20]:
        flag = " [disabled]" if b.get("dis") else ""
        eid = f" ({b.get('eid')})" if b.get("eid") else ""
        print(f"    - {b.get('t')}{flag}{eid}")
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
    sub.add_parser("checkin-now", help="手动跑签到时间线一轮（只签到）")
    sub.add_parser("exchange-now", help="手动跑兑换检查时间线一轮")
    sub.add_parser("travel-now", help="手动跑旅行时间线一轮（领礼物→派遣→读倒计时）")
    sub.add_parser("once", help="手动跑全量一轮（签到+礼物+兑换+旅行）")
    sub.add_parser("status", help="只读状态：三条时间线下次执行时间")
    sub.add_parser("inspect", help="只读诊断：打印客户端/浏览器窗口与关键 AX 文案（跨机器排查）")
    sub.add_parser("daemon", help="统一常驻进程：三条独立时间线")
    sub.add_parser("install-launchd", help="写 LaunchAgent plist（开机常驻，不自动加载）")
    sub.add_parser("model-check", help="show free-model policy and current acknowledgment state")
    # 第三方依赖：安装技能时必跑 deps check，确认 CuaDriver 已安装并授权
    dep = sub.add_parser("deps", help="第三方依赖检查（安装技能时必跑）")
    dep_sub = dep.add_subparsers(dest="action", required=True)
    dep_sub.add_parser("check", help="检查所有第三方依赖是否就绪（默认）")
    dep_sub.add_parser("install", help="打印 GUI 依赖（CuaDriver）的手动安装与授权指引")
    dep_sub.add_parser("list", help="仅列出依赖与路径")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cfg = config.load(args.config)
    logging.getLogger().setLevel(getattr(logging, cfg.get("log_level", "INFO"), logging.INFO))
    # 依赖 WorkBuddy：执行前必须确认运行在免费模型下（model-check / deps /
    # install-launchd / inspect 属本地运维或只读诊断，不消耗模型额度，放行；
    # plist 内已内置 BUDDY_FREE_MODEL=1，daemon 运行时仍受闸门约束）
    if args.command not in ("model-check", "deps", "install-launchd", "inspect"):
        rc = preflight_model_check(cfg)
        if rc:
            return rc
    handlers = {
        "checkin-now": cmd_checkin_now,
        "exchange-now": cmd_exchange_now,
        "travel-now": cmd_travel_now,
        "once": cmd_once,
        "status": cmd_status,
        "inspect": cmd_inspect,
        "daemon": cmd_daemon,
        "install-launchd": cmd_install_launchd,
        "model-check": cmd_model_check,
        "deps": cmd_deps,
    }
    return handlers[args.command](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
