"""第三方依赖的检查与安装引导。

本技能除 Python 标准库外，依赖若干外部 CLI / GUI 应用。**安装技能时**必须由
agent 先运行 `./run.sh deps install` 再 `./run.sh deps check`，确认所有第三方
依赖已安装并在 PATH / 指定路径可用，否则缺失只会在真实执行时才暴露（mock 测不到）。

依赖矩阵（按本机实际调用核对，2026-11-09）：
  - Python          : 运行时（tomllib / sqlite3 / zoneinfo，均为标准库）
  - agent-browser   : 网页端 headless 会话驱动（web.bin，默认在 PATH）
  - CuaDriver       : 成长中心 + 桌面签到（cua-driver 引擎）的 AX 通道，GUI 应用
  - Microsoft Edge  : 成长中心 AX 窗口来源（已登录的个人中心）
  - agent-desktop   : 桌面端 agent-desktop 引擎回退（npm，tools/ 本地安装）
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = ROOT / "tools"
CUA_DEFAULT_BIN = "/Applications/CuaDriver.app/Contents/MacOS/cua-driver"


def _which(name: str) -> Path | None:
    p = shutil.which(name)
    return Path(p) if p else None


def _local_node_bin(name: str) -> Path | None:
    p = TOOLS_DIR / "node_modules" / ".bin" / name
    return p if p.exists() else None


def resolve_bin(name: str) -> Path | None:
    """优先 PATH，其次项目本地 tools/node_modules/.bin（自包含、零系统污染）。"""
    return _which(name) or _local_node_bin(name)


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
    # 取首行里像 0.27.0 / v1.2.0 的版本片段，去掉程序名前缀
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


def agent_browser_dep(cfg: dict) -> dict:
    name = (cfg.get("web") or {}).get("bin", "agent-browser")
    resolved = resolve_bin(name)
    return {
        "name": "agent-browser",
        "kind": "npm-cli",
        "required": True,
        "critical": True,
        "present": resolved is not None,
        "version": _try_version(resolved, "--version") if resolved else None,
        "path": str(resolved) if resolved else name,
        "install": f"cd tools && npm install   # 含 agent-browser；或 `npm install -g {name}`",
        "note": "网页端 headless 会话驱动：优先 PATH，其次 tools/node_modules/.bin",
    }


def cua_driver_dep(cfg: dict) -> dict:
    bin_cfg = (
        (cfg.get("desktop") or {}).get("cua_driver_bin")
        or (cfg.get("growth") or {}).get("cua_driver_bin")
        or CUA_DEFAULT_BIN
    )
    bin_path = Path(str(bin_cfg)).expanduser()
    resolved = bin_path if bin_path.exists() else _which("cua-driver")
    growth_on = bool((cfg.get("growth") or {}).get("enabled", True))
    engine = (cfg.get("desktop") or {}).get("engine", "auto")
    needed = growth_on or engine in ("auto", "cua-driver")
    return {
        "name": "CuaDriver",
        "kind": "gui-app",
        "required": needed,
        "critical": needed,
        "present": resolved is not None,
        "version": _try_version(resolved, "--version") if resolved else None,
        "path": str(resolved) if resolved else bin_cfg,
        "install": (
            "安装 CuaDriver.app（trycua 出品），并到 系统设置→隐私与安全性→辅助功能 "
            "授权其驱动；首次运行 `cua-driver permissions --guide`。"
        ),
        "note": "成长中心与桌面签到（cua-driver 引擎）的 AX 通道依赖它",
    }


def edge_dep(cfg: dict) -> dict:
    growth_on = bool((cfg.get("growth") or {}).get("enabled", True))
    app_name = (cfg.get("growth") or {}).get("browser_app", "Microsoft Edge")
    candidates = [
        f"/Applications/{app_name}.app/Contents/MacOS/{app_name}",
        f"/Applications/{app_name}.app",
    ]
    resolved = next((Path(c) for c in candidates if Path(c).exists()), None)
    return {
        "name": "Microsoft Edge",
        "kind": "gui-app",
        "required": growth_on,
        "critical": growth_on,
        "present": resolved is not None,
        "version": None,
        "path": str(resolved) if resolved else candidates[0],
        "install": "下载安装 Microsoft Edge（成长中心 AX 通道需要其已登录的个人中心窗口）。",
        "note": f"growth.browser_app = {app_name}",
    }


def agent_desktop_dep(cfg: dict) -> dict:
    name = (cfg.get("desktop") or {}).get("bin", "agent-desktop")
    resolved = resolve_bin(name)
    engine = (cfg.get("desktop") or {}).get("engine", "auto")
    required = engine == "agent-desktop"  # auto 时 cua-driver 优先，agent-desktop 仅回退
    return {
        "name": "agent-desktop",
        "kind": "npm-cli",
        "required": required,
        "critical": required,
        "present": resolved is not None,
        "version": _try_version(resolved, "--version") if resolved else None,
        "path": str(resolved) if resolved else name,
        "install": "cd tools && npm install   # package.json 已含 agent-desktop@^1.2.0",
        "note": "桌面端 agent-desktop 引擎回退；cua-driver 可用时非必需",
    }


def collect(cfg: dict) -> list[dict]:
    return [
        python_dep(),
        agent_browser_dep(cfg),
        cua_driver_dep(cfg),
        edge_dep(cfg),
        agent_desktop_dep(cfg),
    ]


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
    """安装可经 npm 管理的依赖（agent-desktop / agent-browser），GUI 依赖只给指引。"""
    print("[deps] 安装经 npm 管理的依赖（tools/）...")
    if TOOLS_DIR.exists():
        try:
            proc = subprocess.run(
                ["npm", "install"], cwd=str(TOOLS_DIR), capture_output=True, text=True, timeout=300
            )
            if proc.returncode == 0:
                print("[deps] npm install 完成（agent-desktop 已就绪）。")
            else:
                print(f"[deps] npm install 返回 {proc.returncode}：\n{proc.stderr[-600:]}", file=sys.stderr)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"[deps] npm install 失败：{exc}", file=sys.stderr)
        # 尝试把 agent-browser 也装到本地（--no-save，不污染 package.json）；失败不致命
        if resolve_bin("agent-browser") is None:
            print("[deps] 尝试本地安装 agent-browser（--no-save）...")
            try:
                proc = subprocess.run(
                    ["npm", "install", "--no-save", "agent-browser"],
                    cwd=str(TOOLS_DIR), capture_output=True, text=True, timeout=300,
                )
                if proc.returncode == 0:
                    print("[deps] agent-browser 已安装到 tools/node_modules/.bin。")
                else:
                    print("[deps] 本地安装 agent-browser 失败，将依赖 PATH 上的 agent-browser；"
                          "若无则请 `npm install -g agent-browser`。", file=sys.stderr)
            except (OSError, subprocess.SubprocessError) as exc:
                print(f"[deps] 本地安装 agent-browser 异常：{exc}（可忽略，依赖 PATH 版本）", file=sys.stderr)
    else:
        print(f"[deps] 未找到 {TOOLS_DIR}，跳过 npm 安装。", file=sys.stderr)

    print("\n[deps] 以下为 GUI 依赖，请手动安装并授权：")
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
    print("   请先运行 `./run.sh deps install` 安装，再 `./run.sh deps check` 复查。")
    return 1
