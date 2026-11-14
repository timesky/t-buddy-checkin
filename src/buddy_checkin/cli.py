"""命令行入口。所有命令默认后台/只读，不接管用户浏览器、不激活窗口。"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from pathlib import Path

from . import config, launchd, ports
from .actions import Actions
from .adapters.fuel_card import FuelCard
from .adapters.growth_browser import GrowthBrowser
from .deps import cmd_deps
from .progress import IMPOSSIBLE, DONE, TASK_LABELS, TASK_ORDER, Progress, default_path
from .scheduler import Scheduler
from .state import State
from .timeutil import format_ts

ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger("buddy-checkin")


def inject_port(cfg: dict) -> ports.Resolution:
    """把「客户端实际用的 CDP 端口」注入配置 —— 全流程唯一的端口来源。

    客户端端口由启动时的环境变量决定，可能与本机 config.toml 不同（另一台
    机器是 9223）。这里先探一遍真实状态，再下发；各适配器不再各自兜底 9222。
    """
    res = ports.resolve(cfg)
    growth = cfg.setdefault("growth", {})
    if res.ok:
        if growth.get("cdp_port") != res.port:
            logger.info("cdp: %s 端口 %s → %s（来源 %s）",
                        ports.ENV_KEY, growth.get("cdp_port"), res.port, res.source)
        growth["cdp_port"] = res.port
    growth["cdp_port_source"] = res.source if res.ok else "none"
    return res


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
    """手动：只做签到任务的一轮（不碰成长中心）。"""
    state, _desktop, actions = build(cfg)
    try:
        return _json(actions.daily_checkin())
    finally:
        state.close()


def cmd_makeup_now(cfg: dict, args: argparse.Namespace) -> int:
    """手动：只做漏签补签（成长中心日历 + 补登卡）。"""
    state, _desktop, actions = build(cfg)
    try:
        return _json(actions.missed_makeup())
    finally:
        state.close()


def cmd_lottery_now(cfg: dict, args: argparse.Namespace) -> int:
    """手动：只做抽奖（把「抽奖(N)」的券抽到 0）。"""
    state, _desktop, actions = build(cfg)
    try:
        return _json(actions.draw_lottery())
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


def _print_ledger(progress: Progress) -> None:
    print("-- 当日任务账本（接续文件）--")
    print(f"  day            : {progress.day}")
    flags = {DONE: "✓ 完成", IMPOSSIBLE: "— 今天做不了", "pending": "… 待重试"}
    for name in TASK_ORDER:
        rec = progress.get(name)
        print(f"  {TASK_LABELS.get(name, name):8}: {flags.get(rec.state, rec.state):<12} "
              f"尝试 {rec.attempts} 次  {rec.detail[:90]}")
    verdict = "全部终态（今天 100% 完成或确实做不了）" if progress.all_terminal() \
        else "仍有任务待重试"
    print(f"  结论           : {verdict}")


def cmd_once(cfg: dict, args: argparse.Namespace) -> int:
    """手动：按账本跑完五个任务一遍（等价于一次重启自检）。"""
    state, _desktop, actions = build(cfg)
    try:
        progress = Progress(default_path(cfg))
        scheduler = Scheduler(cfg, state, actions, progress)
        states = scheduler.verify_pass()
        _print_ledger(progress)
        print("\n" + json.dumps(config.scrub(states), ensure_ascii=False, indent=2))
        return 0 if progress.all_terminal() else 1
    finally:
        state.close()


def cmd_verify(cfg: dict, args: argparse.Namespace) -> int:
    """只读式自检：读真实状态，把账本纠正到真实值（会补做没做的任务）。"""
    return cmd_once(cfg, args)


def cmd_status(cfg: dict, args: argparse.Namespace) -> int:
    state, _desktop, _actions = build(cfg)
    print("== buddy-web-checkin status ==")
    print(f"今日(北京时间)      : {config.scrub(state.kv_get('last_checkin_day')) or '未签到'}")
    g = cfg.get("growth", {}) or {}
    engine = (g.get("engine") or "browser").strip().lower()
    port = g.get("cdp_port", ports.DEFAULT_PORT)
    src = g.get("cdp_port_source", "?")
    print(f"成长中心引擎        : {engine} (cdp_port={port}, 来源 {src})")
    if engine == "browser":
        gb = GrowthBrowser(cfg)
        alive = gb.available()
        page = "已打开" if alive and gb._iframe_ws() else ("未打开" if alive else "-")
        print(f"CDP {port:<16}: {'可达' if alive else '不可达（客户端未带调试端口启动）'}  页面: {page}")

    progress = Progress(default_path(cfg))
    print()
    _print_ledger(progress)
    if progress.silent_until:
        print(f"  静默至         : {format_ts(progress.silent_until)}（次日 0 点翻页恢复）")
    print()
    print("-- 各任务下次检查 --")
    for name in TASK_ORDER:
        due = state.kv_get_float(f"next_{name}_at")
        shown = format_ts(due) if due else "-"
        print(f"  {TASK_LABELS.get(name, name):8} {shown}")
    print("-- 最近运行 --")
    for row in state.recent(10):
        print(f"  {format_ts(row['ts'])}  {'OK' if row['ok'] else 'FAIL'}  {row['action']}: {row['detail'][:100]}")
    state.close()
    return 0


def cmd_daemon(cfg: dict, args: argparse.Namespace) -> int:
    state, _desktop, actions = build(cfg)
    stop = threading.Event()
    progress = Progress(default_path(cfg))
    scheduler = Scheduler(cfg, state, actions, progress)

    def _stop(_signum, _frame):
        stop.set()

    try:
        import signal

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
    except (ImportError, ValueError):
        pass
    # 端口：daemon 是 launchd job，具有 gui 域写权限，能替我们把端口 setenv
    # 并落进 cdp-env plist（agent 自己的 shell 没这个权限，实测 setenv 返回
    # "Not privileged to set domain environment."）。这一步保证「重启电脑后端口不丢」。
    try:
        env_res = ports.ensure_env(cfg)
        logger.info("端口落地：%s", json.dumps(env_res, ensure_ascii=False)[:240])
        if not env_res.get("setenv"):
            logger.warning("setenv 未成功（%s）；下次登录由 plist 自动加载补偿",
                           env_res.get("setenv_error", "?"))
    except Exception as exc:  # noqa: BLE001 - 端口落盘失败不该阻断任务
        logger.warning("端口落地失败：%s", exc)
    # 重启不受静默控制：initial_jobs 让五个任务立刻各跑一遍（真实状态复核），
    # 只有复核后仍全部终态才重新静默。账本里写错的 done 会在这一轮被纠正。
    print("[daemon] started: 五个任务(签到/补签/兑换/抽奖/派遣)立即各复核一遍；"
          " 全部终态后静默到 23:59，次日 0 点翻页 (Ctrl-C 退出)")
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
    port = g.get("cdp_port", ports.DEFAULT_PORT)
    gb = GrowthBrowser(cfg)
    print("== CDP 诊断 ==")
    print(f"  port            : {port}（来源 {g.get('cdp_port_source', '?')}）")
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
            print("    该端口被其它浏览器/Electron 应用占用。先看端口全貌，再协商空闲端口：")
            print("    ./run.sh ports status")
            print("    ./run.sh ports negotiate --apply   # 写端口并提示重启客户端")
            return 1
        print("    先确认客户端在跑且开了调试端口：./run.sh ports status")
        return 1

    fc = FuelCard(cfg)
    fst = fc.probe()
    print("\n== 签到状态（左下角 Buddy加油站 浮层）==")
    if not fst.get("ok"):
        print(f"  外观读取失败: {fst.get('detail')}")
    else:
        card = fst.get("card") or {}
        print(f"  浮层卡片    : {card.get('title') or (fst.get('detail') or '')[:40]}")
        print(f"  副标题      : {card.get('sub') or '-'}")
        print(f"  按钮        : {card.get('btn') or '-'}"
              f"（type={(card.get('props') or {}).get('type')}）")
        print(f"  轮播        : " + ", ".join(
            f"{d.get('label')}{'*' if d.get('on') else ''}" for d in card.get("dots") or []))
    # 权威判据：服务端 checkinData（不是卡片在不在）
    auth = fc.status(refresh=True)
    if auth.get("ok"):
        print(f"  服务端已签  : {auth.get('today_checked_in')}"
              f"（今日积分 {auth.get('today_credit')}，连签 {auth.get('streak_days')} 天）")
        dates = auth.get("checkin_dates") or []
        print(f"  最近签到日  : {', '.join(dates[:4]) or '-'}")
        print(f"  判定        : {'今日已签到' if auth.get('today_checked_in') else '今日未签到（需补点）'}")
    else:
        print(f"  服务端状态读取失败: {auth.get('reason')}")

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
    else:
        print(f"  读取失败: {st.get('detail')}")

    mk = gb.makeup_scan()
    print("\n== 漏签补签 ==")
    if mk.get("ok"):
        items = mk.get("items") or []
        print(f"  可补漏签    : {', '.join(str(i.get('date')) for i in items) or '无'}")
        print(f"  补登卡      : {mk.get('card_text') or mk.get('card_balance')}")
    else:
        print(f"  读取失败: {mk.get('detail')}")
    tickets, label = gb.lottery_tickets()
    print(f"\n== 抽奖 ==\n  抽奖券      : {tickets}（{label or '按钮未找到'}）")

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
    """写 daemon plist（默认不加载），顺带把端口持久化到登录项。

    端口持久化是必须的：手动 `launchctl setenv` 只活在当次登录会话里，
    重启电脑就丢；另一台机器 9223 之所以稳定，是因为它的 cdp-env plist 已加载。
    """
    log_path = config.expand("~/.local/share/buddy-web-checkin/daemon.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    load_now = bool(getattr(args, "load", False))
    res = launchd.install_daemon(ROOT, log_path, do_bootstrap=load_now)
    print(f"[install] wrote {res['path']} (mode {res['mode']})")
    if load_now:
        boot = res.get("bootstrap") or {}
        if boot.get("ok"):
            print("[install] bootstrap OK，守护已加载")
        else:
            print(f"[install] bootstrap 失败：{boot.get('error', '')}")
    else:
        print("[install] 未自动加载。确认后执行：")
        print(f"  launchctl bootstrap gui/$(id -u) {res['path']}")
        print("  （若报 `Bootstrap failed: 5`，多为同 label 已加载：先 "
              f"`launchctl bootout gui/$(id -u)/{launchd.DAEMON_LABEL}` 再 bootstrap）")

    port_res = ports.resolve(cfg)
    port = port_res.port
    pinfo = launchd.install_cdp_env(port)
    print(f"[install] 端口持久化 {pinfo['path']} → {port}")
    if launchd.loaded(launchd.CDP_ENV_LABEL):
        print("[install] cdp-env 已加载：重启电脑后端口自动恢复")
    else:
        print("[install] cdp-env 尚未加载 —— 它会在**下次登录**被 launchd 自动加载。")
        print("          想让本次会话立刻生效，二选一：")
        print(f"            launchctl bootstrap gui/$(id -u) {pinfo['path']}   # 在终端里跑")
        print("            重启 daemon（它启动时会 launchctl setenv，见 daemon 日志）")
    return 0


def cmd_ports(cfg: dict, args: argparse.Namespace) -> int:
    """端口只读报告 / 协商。跨机器排查时把 `ports status` 的输出整段贴回来即可。"""
    action = getattr(args, "action", "status")
    if action == "status":
        rep = ports.report(cfg)
        print(ports.format_report(rep))
        cs = launchd.service_state(launchd.CDP_ENV_LABEL)
        path = launchd.cdp_env_plist_path()
        if cs.get("loaded"):
            persisted = "已加载"
        elif path.exists():
            persisted = "已写入（下次登录由 launchd 自动加载）"
        else:
            persisted = "未写入 —— 重启电脑后端口会丢"
        print(f"  端口持久化      : {persisted}  ({path})")
        if cs.get("loaded") and rep["env"]["port"] is None:
            print("                    ⚠️ plist 已加载但域环境里读不到端口，建议重跑 "
                  "`ports negotiate --apply`")
        return 0 if rep["resolved"]["ok"] else 1

    plan = ports.negotiate(cfg)
    pin_port = getattr(args, "port", None)
    if pin_port:
        plan = ports.pin(cfg, int(pin_port))
    print(f"  建议     : {plan['action']}  端口 {plan['port']}")
    print(f"  原因     : {plan['reason']}")
    if plan.get("needs_restart"):
        print("  生效条件 : 需要重启 WorkBuddy 客户端（本工具不会自动重启，避免打断会话）")
    apply_now = bool(getattr(args, "apply", False))
    if not apply_now:
        print("\n  （dry-run：未改动任何文件。加 --apply 落盘）")
        return 1 if plan["action"] in ("client_not_running", "no_free_port") else 0
    if plan["action"] in ("client_not_running", "no_free_port"):
        print("  未落盘：先满足上面的前置条件")
        return 1
    done = ports.apply_port(cfg, plan["port"])
    print("\n  已落盘：")
    setenv_ok = bool(done.get("setenv"))
    print(f"    launchctl setenv : {'OK' if setenv_ok else '未生效 ' + done.get('setenv_error', '')}")
    if not setenv_ok:
        print("      （agent 进程没有 gui 域写权限是正常的：daemon 启动时会补 setenv，"
              "或下次登录由 plist 自动加载）")
    pl = done.get("plist") or {}
    print(f"    cdp-env plist    : {'OK' if pl.get('ok') else '失败 ' + str(pl.get('error', ''))}  "
          f"{pl.get('path', '')}")
    print(f"    config.toml      : {'已更新' if done.get('config') else '未改动'}  "
          f"{done.get('config_path', '')}")
    if plan.get("needs_restart"):
        print("\n  下一步：重启 WorkBuddy 客户端，然后 ./run.sh ports status 复核")
    return 0



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="buddy-checkin")
    parser.add_argument("--config", help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("checkin-now", help="手动跑签到任务一轮（只签到）")
    sub.add_parser("makeup-now", help="手动跑漏签补签（成长中心日历 + 补登卡）")
    sub.add_parser("exchange-now", help="手动跑兑换任务一轮")
    sub.add_parser("lottery-now", help="手动跑抽奖任务一轮")
    sub.add_parser("travel-now", help="手动跑派遣任务一轮（领礼物→派遣→读倒计时）")
    sub.add_parser("once", help="按账本跑完五个任务一遍（= 一次重启自检）")
    sub.add_parser("verify", help="自检：用真实状态纠正账本并补做未完成任务")
    sub.add_parser("status", help="只读状态：账本 + 各任务下次检查时间")
    sub.add_parser("inspect", help="只读诊断：CDP/签到服务端状态/补签/抽奖（跨机器排查）")
    sub.add_parser("daemon", help="统一常驻进程：五个任务滚动，全终态静默到 23:59")
    il = sub.add_parser("install-launchd", help="写 LaunchAgent plist（端口持久化 + 开机常驻）")
    il.add_argument("--load", action="store_true", help="写完直接 bootstrap 加载（默认只写）")
    sub.add_parser("model-check", help="show free-model policy and current acknowledgment state")
    # 端口：只读报告 / 协商空闲端口
    prt = sub.add_parser("ports", help="CDP 调试端口：status 只读报告 / negotiate 协商空闲端口")
    prt_sub = prt.add_subparsers(dest="action", required=True)
    prt_sub.add_parser("status", help="打印端口全貌与判定（跨机器排查时贴这段）")
    neg = prt_sub.add_parser("negotiate", help="给出建议端口；加 --apply 才落盘")
    neg.add_argument("--apply", action="store_true", help="真正写入 setenv + plist + config.toml")
    neg.add_argument("--port", type=int, help="指定端口（脚本传参用）；不给则自动挑空闲端口")
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
    if args.command not in ("model-check", "deps", "install-launchd", "inspect", "ports"):
        rc = preflight_model_check(cfg)
        if rc:
            return rc
    # 端口：任何需要 CDP 的命令都先探真实端口再下发（唯一注入点）
    if args.command not in ("model-check", "ports"):
        inject_port(cfg)
    handlers = {
        "checkin-now": cmd_checkin_now,
        "makeup-now": cmd_makeup_now,
        "exchange-now": cmd_exchange_now,
        "lottery-now": cmd_lottery_now,
        "travel-now": cmd_travel_now,
        "once": cmd_once,
        "verify": cmd_verify,
        "status": cmd_status,
        "inspect": cmd_inspect,
        "daemon": cmd_daemon,
        "install-launchd": cmd_install_launchd,
        "ports": cmd_ports,
        "model-check": cmd_model_check,
        "deps": cmd_deps,
    }
    return handlers[args.command](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
