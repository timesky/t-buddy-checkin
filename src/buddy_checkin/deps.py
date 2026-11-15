"""第三方依赖的检查与安装引导。

当前运行时链路（2026-11-12 起）：**agent-browser + WorkBuddy 客户端 CDP**。

  - 成长中心是客户端内嵌的 OOPIF，必须通过 CDP 连接才能做 DOM 级操作
  - 打开页面入口（侧栏「成长计划」→「立即前往」）靠 agent-browser 的跨
    webContents 聚合 snapshot/click
  - 客户端需带 WORKBUDDY_REMOTE_DEBUGGING_PORT 环境变量启动，才会暴露调试端口

必需依赖：WorkBuddy 客户端 ≥5.11（5.9.x 是网页版活动中心，不支持）、
Python ≥3.11、node ≥22、agent-browser、客户端调试端口。
可选依赖：agent-desktop（AX 兜底，不参与日常流程）。

旧的 CuaDriver / AX 通道已整体移除，不再列为依赖。
安装技能时由 agent 先跑 `./run.sh deps check`；缺失只在真实执行时才暴露。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _which(name: str) -> Path | None:
    import shutil

    p = shutil.which(name)
    return Path(p) if p else None


def _bin_env() -> dict[str, str]:
    """补 PATH 再跑子进程。

    agent-browser 的 shebang 是 ``#!/usr/bin/env node``，launchd 启动时 PATH 只有
    /usr/bin:/bin:/usr/sbin:/sbin，会报 "env: node: No such file or directory"，
    导致 deps 把已安装的 agent-browser 显示成缺失（2026-11-12 实测）。
    """
    env = dict(os.environ)
    path = env.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin"
    extra: list[str] = []
    try:
        from .adapters.growth_browser import GrowthBrowser

        node = GrowthBrowser._resolve_node()
        if node:
            extra.append(str(Path(node).parent))
    except Exception:
        pass
    for p in ("/opt/homebrew/bin", "/usr/local/bin"):
        if p not in path and p not in extra:
            extra.append(p)
    env["PATH"] = ":".join([p for p in extra if p] + [path])
    return env


def _try_version(bin_path: Path | str, *args: str, timeout: int = 8) -> str | None:
    try:
        proc = subprocess.run(
            [str(bin_path), *args], capture_output=True, text=True, timeout=timeout,
            env=_bin_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or proc.stderr or "").strip()
    if not out:
        return None
    m = re.search(r"\d+\.\d+(?:\.\d+)?", out.splitlines()[0])
    return m.group(0) if m else out.splitlines()[0].strip()[:60]


def python_dep() -> dict:
    vi = sys.version_info
    present = vi >= (3, 11)
    version = f"{vi.major}.{vi.minor}.{vi.micro}"
    return {
        "name": "Python",
        "kind": "runtime",
        "required": True,
        "critical": True,
        "present": present,
        "version": version,
        "path": sys.executable,
        "install": "使用 Python ≥ 3.11（推荐 3.13）：https://www.python.org/downloads/",
        "note": "需要 tomllib / sqlite3 / zoneinfo（均为标准库）",
    }


def node_dep() -> dict:
    from .adapters.growth_browser import GrowthBrowser

    resolved = GrowthBrowser._resolve_node()
    version = _try_version(resolved, "--version") if resolved else None
    major = None
    if version:
        m = re.match(r"(\d+)\.", version)
        major = int(m.group(1)) if m else None
    # node ≥22 是硬要求：tools/cdp_client.mjs 用全局 WebSocket（Node 22 才稳定
    # 可用，20/21 上没有），agent-browser 自己也声明 engines.node >=22。
    present = bool(resolved) and (major is None or major >= 22)
    return {
        "name": "node",
        "kind": "runtime",
        "required": True,
        "critical": True,
        "present": present,
        "version": version,
        "path": resolved or "未找到（PATH 或 ~/.workbuddy/binaries/node）",
        "install": (
            "升级到 Node.js ≥ 22（brew install node@22 / nvm install 22），"
            "或直接用 WorkBuddy 自带的 ~/.workbuddy/binaries/node"
        ),
        "note": (
            "tools/cdp_client.mjs 用全局 WebSocket，需 Node ≥ 22"
            + (f"；当前 {version} 不满足" if present is False and resolved else "")
        ),
    }


def client_version_dep(cfg: dict) -> dict:
    """WorkBuddy 客户端版本：5.11 以下一定跑不起来（不是「可能有问题」）。"""
    from . import compat, ports

    ver = ports.client_version()
    res = compat.check(ver)
    # 只有「能证明过期」才判红；读不到版本（装在自定义路径）不算失败，
    # 否则会把能正常用的机器误报成缺依赖。
    return {
        "name": "WorkBuddy 客户端",
        "kind": "app-version",
        "required": True,
        "critical": True,
        "present": bool(res["ok"]),
        "version": ver,
        "path": str(ports.client_app() or "未找到 WorkBuddy.app（/Applications 或 ~/Applications）"),
        "install": compat.upgrade_text(ver),
        "note": res["message"],
    }


def agent_browser_dep(cfg: dict) -> dict:
    from .adapters.growth_browser import GrowthBrowser

    g = cfg.get("growth", {}) or {}
    resolved = GrowthBrowser._resolve_agent_browser(g)
    return {
        "name": "agent-browser",
        "kind": "npm-tool",
        "required": True,
        "critical": True,
        "present": bool(resolved),
        "version": _try_version(resolved, "--version") if resolved else None,
        "path": resolved or str(PROJECT_ROOT / "tools" / "node_modules" / ".bin" / "agent-browser"),
        "install": "cd <项目>/tools && npm install",
        "note": "跨 webContents 聚合 snapshot/click，用于从客户端侧栏打开成长中心页面",
    }


def cdp_dep(cfg: dict) -> dict:
    """WorkBuddy 客户端调试端口：CDP 链路的运行前提。"""
    from . import ports
    from .adapters.growth_browser import GrowthBrowser

    g = cfg.get("growth", {}) or {}
    port = ports.config_port(cfg)
    source = g.get("cdp_port_source") or "config"
    gb = GrowthBrowser(cfg)
    alive = gb.available()
    blocked = gb.block[0] if gb.block else None
    note = "客户端暴露的 DevTools 端口；未开启时无法读取成长中心页面"
    if alive:
        note = f"已连通（端口来源 {source}）"
    if blocked == "cdp_port_foreign":
        note = (f"端口 {port} 被其它浏览器占用（{gb.block[1]}）；"
                "跑 ./run.sh ports status 看全貌，或 ports negotiate --apply 换端口")
    elif not alive:
        note = ("客户端未暴露调试端口。先跑 ./run.sh ports status；"
                "需要写端口时用 ./run.sh ports negotiate --apply，再重启客户端")
    return {
        "name": "WorkBuddy CDP",
        "kind": "runtime-port",
        "required": True,
        "critical": True,
        "present": alive,
        "version": None,
        "path": f"127.0.0.1:{port}",
        "install": (
            f"launchctl setenv WORKBUDDY_REMOTE_DEBUGGING_PORT {port} && "
            "重启 WorkBuddy 客户端（或跑 ./run.sh install-launchd 持久化端口）"
        ),
        "note": note,
    }


def agent_desktop_dep(cfg: dict) -> dict:
    """agent-desktop（可选）：AX 兜底工具，严格 headless 不抢焦点/光标。"""
    desk = cfg.get("desktop", {}) or {}
    configured = desk.get("agent_desktop_bin") or "node_modules/.bin/agent-desktop"
    local = PROJECT_ROOT / "tools" / configured
    resolved = local if local.exists() else (
        _which(Path(configured).name) if not Path(configured).is_absolute()
        else Path(configured) if Path(configured).exists() else None
    )
    return {
        "name": "agent-desktop",
        "kind": "npm-tool",
        "required": False,
        "critical": False,
        "present": resolved is not None,
        "version": _try_version(resolved, "--version") if resolved else None,
        "path": str(resolved) if resolved else str(local),
        "install": (
            "cd <项目>/tools && npm install；再把「启动它的 App」加入 "
            "系统设置→隐私与安全性→辅助功能（macOS 27+ 设备控制和数据访问）"
        ),
        "note": "可选 AX 兜底（CDP 不可达时人工排查用），不参与日常流程",
    }


def collect(cfg: dict) -> list[dict]:
    # 顺序 = 排查顺序：先看业务前提（客户端版本），再看运行时，最后看端口与可选件
    return [
        client_version_dep(cfg),
        python_dep(),
        node_dep(),
        agent_browser_dep(cfg),
        cdp_dep(cfg),
        agent_desktop_dep(cfg),
    ]


def evaluate(cfg: dict) -> dict:
    deps = collect(cfg)
    required_missing = [d for d in deps if d["required"] and not d["present"]]
    optional_missing = [d for d in deps if not d["required"] and not d["present"]]
    from . import compat

    return {
        "ok": not required_missing,
        "dependencies": deps,
        "required_missing": [d["name"] for d in required_missing],
        "optional_missing": [d["name"] for d in optional_missing],
        "client_compat": compat.check(
            next((d["version"] for d in deps if d["name"] == "WorkBuddy 客户端"), None)
        ),
    }


def install(cfg: dict) -> int:
    print("[deps] 运行前提：WorkBuddy 客户端 ≥ 5.11 + node ≥ 22 + agent-browser + CDP 端口。\n")
    for d in collect(cfg):
        status = "已就绪" if d["present"] else "缺失"
        print(f"  - {d['name']} [{status}]")
        print(f"      {d['note']}")
        if not d["present"]:
            for line in str(d["install"]).splitlines():
                print(f"      {line}")
    return 0


def cmd_deps(cfg: dict, args: Any) -> int:
    action = getattr(args, "action", "check")
    if action == "install":
        return install(cfg)
    report = evaluate(cfg)
    if action == "list":
        for d in report["dependencies"]:
            print(f"{d['name']:16} {('OK' if d['present'] else 'MISSING'):8} "
                  f"{('[required]' if d['required'] else '[optional]'):12} {d['path']}")
        return 0
    print("== buddy-web-checkin 依赖检查 ==")
    for d in report["dependencies"]:
        flag = "OK" if d["present"] else ("MISSING" if d["required"] else "可选-未装")
        ver = f" v{d['version']}" if d.get("version") else ""
        print(f"  [{flag:9}] {d['name']}{ver}")
        print(f"       {d['note']}")
        if not d["present"]:
            print("       ↓ 处理方式")
            for line in str(d["install"]).splitlines():
                print(f"       {line}")
    print("")
    if report["ok"]:
        print("✅ 所有必需依赖就绪，可以运行 ./run.sh once / daemon。")
        return 0
    print(f"❌ 缺失必需依赖：{', '.join(report['required_missing'])}")
    if report["optional_missing"]:
        print(f"⚠️  可选依赖缺失（不影响主流程）：{', '.join(report['optional_missing'])}")
    return 1
