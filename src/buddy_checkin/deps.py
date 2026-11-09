"""第三方依赖的检查与安装引导。

本技能除 Python 标准库外，只依赖**一个**外部工具：**CuaDriver**（trycua 出品的 AX 驱动）。
它是签到（WorkBuddy 桌面客户端）与成长中心（已登录的个人中心浏览器窗口）的唯一通道，
也是本机唯一拿到 TCC 辅助功能授权的后端（auth=2），其它候选（agent-browser /
agent-desktop / cdp / computer-use）要么未授权、要么已废弃，**均未使用**，故不列入依赖。

安装技能时由 agent 先跑 `./run.sh deps check`，确认 CuaDriver 已安装并在辅助功能里授权；
缺失只在真实执行时才暴露（mock 单测测不到）。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

CUA_DEFAULT_BIN = "/Applications/CuaDriver.app/Contents/MacOS/cua-driver"


def _which(name: str) -> Path | None:
    import shutil

    p = shutil.which(name)
    return Path(p) if p else None


def _try_version(bin_path: Path | str, *args: str, timeout: int = 8) -> str | None:
    try:
        proc = subprocess.run(
            [str(bin_path), *args], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or proc.stderr or "").strip()
    if not out:
        return None
    # 取首行里像 0.14.1 / v1.2.0 的版本片段，去掉程序名前缀
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


def cua_driver_dep(cfg: dict) -> dict:
    bin_cfg = (
        (cfg.get("desktop") or {}).get("cua_driver_bin")
        or (cfg.get("growth") or {}).get("cua_driver_bin")
        or CUA_DEFAULT_BIN
    )
    bin_path = Path(str(bin_cfg)).expanduser()
    resolved = bin_path if bin_path.exists() else _which("cua-driver")
    return {
        "name": "CuaDriver",
        "kind": "gui-app",
        "required": True,
        "critical": True,
        "present": resolved is not None,
        "version": _try_version(resolved, "--version") if resolved else None,
        "path": str(resolved) if resolved else bin_cfg,
        "install": (
            "安装 CuaDriver.app（trycua 出品），并到 系统设置→隐私与安全性→辅助功能 "
            "授权其驱动；首次运行 `cua-driver permissions --guide`。"
        ),
        "note": "签到（桌面客户端）与成长中心（浏览器窗口）的唯一 AX 通道",
    }


def browser_dep(cfg: dict) -> dict:
    """成长中心不限定浏览器：你用惯用的浏览器从 WorkBuddy 点开「个人中心」即可。"""
    configured = (cfg.get("growth") or {}).get("browser_app", "auto")
    candidates = [
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Safari.app",
    ]
    resolved = next((Path(c) for c in candidates if Path(c).exists()), None)
    return {
        "name": "浏览器（任意已装）",
        "kind": "gui-app",
        "required": False,
        "critical": False,
        "present": resolved is not None,
        "version": None,
        "path": str(resolved) if resolved else "未检测到常见浏览器",
        "install": "成长中心不限定浏览器：用惯用浏览器从 WorkBuddy 点开「个人中心」即可，"
                   "无需专门安装。",
        "note": f"growth.browser_app = {configured}（auto = 任意浏览器皆可）",
    }


def collect(cfg: dict) -> list[dict]:
    return [python_dep(), cua_driver_dep(cfg), browser_dep(cfg)]


def evaluate(cfg: dict) -> dict:
    deps = collect(cfg)
    required_missing = [d for d in deps if d["required"] and not d["present"]]
    optional_missing = [d for d in deps if not d["required"] and not d["present"]]
    return {
        "ok": not required_missing,
        "dependencies": deps,
        "required_missing": [d["name"] for d in required_missing],
        "optional_missing": [d["name"] for d in optional_missing],
    }


def install(cfg: dict) -> int:
    """唯一 GUI 依赖是 CuaDriver，无 npm / 包管理器依赖，这里只给手动安装与授权指引。"""
    print("[deps] 本技能无 npm 依赖；外部工具只有 CuaDriver（GUI 应用）。\n")
    for d in collect(cfg):
        if d["kind"] == "gui-app":
            status = "已安装" if d["present"] else "缺失"
            print(f"  - {d['name']} [{status}]: {d['install']}")
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
    # default: check
    print("== buddy-web-checkin 第三方依赖检查 ==")
    for d in report["dependencies"]:
        flag = "OK" if d["present"] else ("MISSING" if d["required"] else "missing(可选)")
        ver = f" v{d['version']}" if d.get("version") else ""
        print(f"  [{flag:18}] {d['name']}{ver}")
        print(f"       {d['note']}")
        if not d["present"]:
            print(f"       安装: {d['install']}")
    print("")
    if report["ok"]:
        print("✅ 所有必需依赖就绪，可以运行 ./run.sh once / daemon。")
        return 0
    print(f"❌ 缺失必需依赖：{', '.join(report['required_missing'])}")
    if report["optional_missing"]:
        print(f"⚠️  可选依赖缺失（不影响主流程）：{', '.join(report['optional_missing'])}")
    print("   请先按上方指引安装并授权 CuaDriver，再 `./run.sh deps check` 复查。")
    return 1
