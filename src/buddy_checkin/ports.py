"""CDP 调试端口：发现、归属校验与协商。

事实来源（2026-11-14 读 `app.asar` + 两台 5.11.3 实测）：

  - 客户端启动时只读一次 `WORKBUDDY_REMOTE_DEBUGGING_PORT`，正则 `/^\\d+$/`，
    然后 `appendSwitch('remote-debugging-port', n)`。**端口被占没有任何回退**，
    也不接受 CLI 参数。
  - Chromium 会把真实监听端口写进 `<userData>/DevToolsActivePort`
    （首行端口，次行 ws 路径），所以它才是「运行中客户端听在哪」的权威。
  - 因此：环境变量 = 下次启动的**意图**；DevToolsActivePort = 当前的**事实**；
    两者都可能陈旧，必须用 `/json/version` 的 UA 复核。

归属判据（避开两个已知误判）：
  进程名叫 Electron、路径含 `~/.workbuddy` 都不能作为证据，
  只看 `lsof -p PID -a -d txt` 的可执行文件是否落在 `WorkBuddy.app/Contents/MacOS/`。

只读优先：`resolve()` / `report()` 不改任何文件；只有 `apply_port()` 会写入，
并且需要显式调用。
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ENV_KEY = "WORKBUDDY_REMOTE_DEBUGGING_PORT"
DEFAULT_PORT = 9222
DEFAULT_RANGE = tuple(range(9222, 9243))  # 9222..9242
APP_MARK = "WorkBuddy.app/Contents/MacOS/"


# --------------------------------------------------------------------------
# 事实源读取
# --------------------------------------------------------------------------
def state_dir(cfg: dict | None = None) -> Path:
    """运行数据目录：跟着 state_db 走，保持单机单一目录。"""
    raw = (cfg or {}).get("state_db") or "~/.local/share/buddy-web-checkin/state.sqlite3"
    return Path(str(raw)).expanduser().resolve().parent


def cache_path(cfg: dict | None = None) -> Path:
    return state_dir(cfg) / "cdp_port.json"


def active_port_paths() -> list[Path]:
    """WorkBuddy 自己的 userData 里的端口文件（固定路径，绝不全局 find）。

    2026-11-14 另一台机器实测：`~/Library/Application Support` 下还有
    AdsPower（59680）等第三方浏览器写的 DevToolsActivePort。全局搜索会把
    别人的端口当成客户端的，必须按路径白名单读。
    """
    home = Path.home()
    return [
        home / ".workbuddy" / "app" / "session" / "DevToolsActivePort",
        home / "Library" / "Application Support" / "WorkBuddy" / "DevToolsActivePort",
    ]


def read_active_port() -> dict | None:
    """读客户端自报的端口；格式不对或不存在返回 None（不猜）。"""
    for path in active_port_paths():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            mtime = path.stat().st_mtime
        except OSError:
            continue
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines or not lines[0].isdigit():
            continue
        return {
            "port": int(lines[0]),
            "ws": lines[1] if len(lines) > 1 else "",
            "path": str(path),
            "age_sec": int(max(0, time.time() - mtime)),
        }
    return None


def env_port() -> int | None:
    """读 gui 域环境变量。

    注意 `launchctl getenv` 在部分上下文返回空（2026-11-14 实测踩过），
    必须用 `launchctl print gui/<uid>` 的输出解析。
    """
    try:
        out = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(rf"{ENV_KEY}\s*=>\s*(\d+)", out or "")
    return int(m.group(1)) if m else None


def client_pids() -> list[int]:
    """客户端主进程（Electron 主进程，窗口就挂在它上面）。"""
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "/Applications/WorkBuddy.app/Contents/MacOS/Electron"],
            capture_output=True, text=True, timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(x) for x in (proc.stdout or "").split() if x.isdigit()]


def client_version() -> str | None:
    try:
        proc = subprocess.run(
            ["defaults", "read", "/Applications/WorkBuddy.app/Contents/Info.plist",
             "CFBundleShortVersionString"],
            capture_output=True, text=True, timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    ver = (proc.stdout or "").strip()
    return ver or None


# --------------------------------------------------------------------------
# 端口探测
# --------------------------------------------------------------------------
def http_json(port: int, path: str, timeout: float = 0.5) -> Any:
    url = f"http://127.0.0.1:{int(port)}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def probe(port: int, timeout: float = 0.5) -> dict:
    """端口归类：owned（WorkBuddy CDP）/ foreign（别人）/ closed（没人听）。"""
    port = int(port)
    try:
        ver = http_json(port, "/json/version", timeout)
    except Exception as exc:  # noqa: BLE001 - 任何失败都等价于不可用
        return {
            "port": port, "state": "closed", "ua": "", "browser": "",
            "detail": f"{type(exc).__name__}: {exc}"[:140],
        }
    data = ver if isinstance(ver, dict) else {}
    ua = str(data.get("User-Agent") or "")
    browser = str(data.get("Browser") or "")
    if "workbuddy" in ua.lower():
        state = "owned"
        detail = ua[:80]
    elif not ua:
        # 拿到响应但没有 UA：交给 lsof 的可执行路径定归属，不在这里下结论
        state = "unknown"
        detail = "no user-agent"
    else:
        state = "foreign"
        detail = f"{browser or 'unknown'} | {ua[:70]}"
    return {"port": port, "state": state, "ua": ua, "browser": browser, "detail": detail}


def _lsof(args: list[str], timeout: float = 4.0) -> str:
    try:
        proc = subprocess.run(["lsof", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout or ""


def listener(port: int) -> dict | None:
    """端口上的监听进程：pid / 命令名 / 可执行路径 / 是否 WorkBuddy。"""
    port = int(port)
    out = _lsof(["-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"])
    pid = None
    cmd = ""
    for line in out.splitlines():
        if line.startswith("p"):
            pid = int(line[1:]) if line[1:].isdigit() else None
        elif line.startswith("c"):
            cmd = line[1:]
        if pid and cmd:
            break
    if not pid:
        return None
    exe = ""
    txt = _lsof(["-p", str(pid), "-a", "-d", "txt", "-Fn"], timeout=3.0)
    for line in txt.splitlines():
        if line.startswith("n"):
            exe = line[1:]
            break
    return {
        "port": port,
        "pid": pid,
        "cmd": cmd,
        "exe": exe,
        "owned": APP_MARK in exe,
    }


def owner_state(port: int, prb: dict | None = None) -> tuple[str, str]:
    """综合 /json/version 与 lsof 给最终归属。

    返回 (owned|foreign|closed, 说明)。UA 明确时以 UA 为准；UA 缺失时用
    lsof 的可执行路径兜底（这正是「进程叫 Electron」误判的解法）。
    """
    prb = prb or probe(port)
    if prb["state"] == "owned":
        return "owned", prb["detail"]
    if prb["state"] == "foreign":
        return "foreign", prb["detail"]
    info = listener(port)
    if not info:
        return "closed", prb["detail"] or "no listener"
    if info["owned"]:
        return "owned", f"lsof exe={info['exe']} pid={info['pid']}"
    return "foreign", f"{info['cmd']} pid={info['pid']} exe={info['exe'] or '?'}"


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", int(port)))
        except OSError:
            return False
    return True


def port_range(cfg: dict | None = None) -> list[int]:
    raw = ((cfg or {}).get("growth") or {}).get("cdp_port_range")
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        try:
            lo, hi = int(raw[0]), int(raw[1])
            return list(range(lo, hi + 1))
        except (TypeError, ValueError):
            pass
    return list(DEFAULT_RANGE)


def find_free(cfg: dict | None = None) -> int | None:
    for port in port_range(cfg):
        if port_free(port) and probe(port, timeout=0.2)["state"] == "closed":
            return port
    return None


# --------------------------------------------------------------------------
# 缓存：命中就少探测一轮（只是提示，不做证据）
# --------------------------------------------------------------------------
def read_cache(cfg: dict | None = None) -> dict:
    try:
        return json.loads(cache_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_cache(cfg: dict | None, port: int, source: str) -> None:
    path = cache_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"port": int(port), "source": source, "checked_at": int(time.time())},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        pass


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------
@dataclass
class Resolution:
    port: int
    ok: bool
    source: str
    detail: str = ""
    facts: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"port": self.port, "ok": self.ok, "source": self.source,
                "detail": self.detail, "facts": self.facts}


def config_port(cfg: dict | None) -> int:
    raw = ((cfg or {}).get("growth") or {}).get("cdp_port", DEFAULT_PORT)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PORT


def _candidates(cfg: dict | None, active: dict | None, env: int | None) -> list[tuple[int, str]]:
    """优先级：自报文件 → 环境变量 → 缓存 → 配置 → 区间扫描。"""
    out: list[tuple[int, str]] = []
    if active and active.get("port"):
        out.append((int(active["port"]), "active_file"))
    if env:
        out.append((int(env), "env"))
    cached = read_cache(cfg).get("port")
    if isinstance(cached, int):
        out.append((cached, "cache"))
    out.append((config_port(cfg), "config"))
    seen: set[int] = set()
    uniq: list[tuple[int, str]] = []
    for port, src in out:
        if port in seen:
            continue
        seen.add(port)
        uniq.append((port, src))
    return uniq


def resolve(cfg: dict | None = None, timeout: float = 0.5) -> Resolution:
    """找到运行中客户端的 CDP 端口。

    只读。找不到时返回 ok=False，port 给「下次启动最可能用的那个」（环境变量
    优先），方便调用方在提示里说人话。
    """
    active = read_active_port()
    env = env_port()
    tried: dict[str, str] = {}
    for port, src in _candidates(cfg, active, env):
        prb = probe(port, timeout=timeout)
        state, detail = owner_state(port, prb)
        tried[f"{port}:{src}"] = f"{state} ({detail})"
        if state == "owned":
            write_cache(cfg, port, src)
            return Resolution(
                port=port, ok=True, source=src, detail=detail,
                facts={"active_file": active, "env": env, "tried": tried},
            )
    # 区间扫描：配置和环境都没命中时，兜底找「WorkBuddy 自己开着但我们不知道」的端口
    known = {p for p, _ in _candidates(cfg, active, env)}
    for port in port_range(cfg):
        if port in known:
            continue
        info = listener(port)
        if not info or not info["owned"]:
            continue
        state, detail = owner_state(port, probe(port, timeout=0.3))
        tried[f"{port}:scan"] = f"{state} ({detail})"
        if state == "owned":
            write_cache(cfg, port, "scan")
            return Resolution(
                port=port, ok=True, source="scan", detail=detail,
                facts={"active_file": active, "env": env, "tried": tried},
            )
    preferred = int(env or config_port(cfg))
    return Resolution(
        port=preferred, ok=False, source="none",
        detail="未发现 WorkBuddy 的 CDP 端口（客户端未开调试端口或已退出）",
        facts={"active_file": active, "env": env, "tried": tried,
               "client_pids": client_pids()},
    )


# --------------------------------------------------------------------------
# 只读报告（跨机器排查用，输出可直接贴回）
# --------------------------------------------------------------------------
def report(cfg: dict | None = None) -> dict:
    active = read_active_port()
    env = env_port()
    cfg_port = config_port(cfg)
    pids = client_pids()
    rows = []
    ports = {cfg_port}
    if env:
        ports.add(int(env))
    if active and active.get("port"):
        ports.add(int(active["port"]))
    for port in sorted(ports):
        prb = probe(port)
        state, detail = owner_state(port, prb)
        info = listener(port)
        rows.append({
            "port": port,
            "state": state,
            "detail": detail,
            "listener": f"{info['cmd']} pid={info['pid']}" if info else "",
            "pid": info["pid"] if info else None,
            "exe_owned": bool(info and info["owned"]),
        })
    res = resolve(cfg)
    return {
        "machine": os.uname().nodename,
        "client_version": client_version(),
        "client_pids": pids,
        "env": {"key": ENV_KEY, "port": env},
        "active_file": active,
        "config_port": cfg_port,
        "port_range": [port_range(cfg)[0], port_range(cfg)[-1]],
        "probes": rows,
        "resolved": res.as_dict(),
    }


def format_report(rep: dict) -> str:
    """把 report() 渲染成给人看的段落。"""
    lines = ["== CDP 端口 =="]
    lines.append(f"  机器            : {rep['machine']}  WorkBuddy {rep['client_version'] or '?'}")
    res = rep["resolved"]
    # pgrep -f 会同时命中 --prewarm 之类的 CLI 辅助进程，直接列首项会指错人；
    # 优先报「真正监听解析端口」的那个 PID。
    listen_pid = next(
        (r["pid"] for r in rep["probes"] if r["port"] == res.get("port") and r.get("pid")),
        None,
    )
    pids = rep["client_pids"]
    if listen_pid:
        note = f"（监听 {res['port']} 的进程"
        note += f"；同名 Electron 进程共 {len(pids)} 个，含 CLI/prewarm 辅助）" \
            if len(pids) > 1 else "）"
        lines.append(f"  客户端进程      : {listen_pid}{note}")
    else:
        lines.append(f"  客户端进程      : {', '.join(map(str, pids)) if pids else '未运行'}")
    env = rep["env"]["port"]
    lines.append(f"  {ENV_KEY} : {env if env else '(未设置)'}")
    act = rep["active_file"]
    if act:
        lines.append(f"  自报端口文件    : {act['port']}  ({act['age_sec']}s 前更新)")
        lines.append(f"                    {act['path']}")
    else:
        lines.append("  自报端口文件    : 无")
    lines.append(f"  配置 cdp_port   : {rep['config_port']}  (区间 {rep['port_range'][0]}-{rep['port_range'][1]})")
    lines.append("  端口探测：")
    for row in rep["probes"]:
        mark = {"owned": "WorkBuddy ✓", "foreign": "非 WorkBuddy ✗",
                "closed": "无监听", "unknown": "UA 缺失"}[row["state"]]
        lines.append(f"    {row['port']:<6} {mark:<14} {row['detail'][:70]}")
    if res["ok"]:
        lines.append(f"  判定            : 使用 {res['port']}（来源 {res['source']}）")
    else:
        lines.append(f"  判定            : ✗ {res['detail']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 协商（只读部分）
# --------------------------------------------------------------------------
def negotiate(cfg: dict | None = None) -> dict:
    """给出该不该换端口、换成哪个、要不要重启客户端。

    不改任何东西；`apply=True` 的动作由 CLI 显式触发。
    """
    res = resolve(cfg)
    if res.ok:
        return {
            "action": "keep", "port": res.port, "source": res.source,
            "needs_restart": False,
            "reason": f"运行中的客户端在 {res.port}（来源 {res.source}），无需改动",
        }
    pids = res.facts["client_pids"] if "client_pids" in (res.facts or {}) else client_pids()
    if not pids:
        return {
            "action": "client_not_running", "port": res.port, "source": "none",
            "needs_restart": False,
            "reason": "客户端没在运行；先启动 WorkBuddy（端口环境变量已就绪时会自动生效）",
        }
    conflict = _conflict_of(cfg, res)
    if conflict:
        target = find_free(cfg)
        if target is None:
            return {
                "action": "no_free_port", "port": res.port, "source": "none",
                "needs_restart": False,
                "reason": f"端口区间内没有空闲端口（冲突：{conflict}）",
            }
        return {
            "action": "switch", "port": target, "source": "negotiate",
            "needs_restart": True, "blocked_by": conflict, "previous": res.port,
            "reason": f"{conflict} 占用了 {res.port}，改到空闲端口 {target}",
        }
    target = find_free(cfg)
    return {
        "action": "restart_required", "port": target or res.port, "source": "none",
        "needs_restart": True,
        "reason": ("客户端在跑但没有开调试端口：多半是启动时环境变量未生效。"
                   "写入端口后重启客户端即可"),
    }


def pin(cfg: dict | None, port: int) -> dict:
    """指定端口（脚本传参用）：能钉就钉，被别人占就明确拒绝。"""
    port = int(port)
    state, detail = owner_state(port, probe(port, timeout=0.3))
    if state == "owned":
        return {"action": "keep", "port": port, "source": "pinned",
                "needs_restart": False, "reason": f"客户端已经在 {port} 上（{detail}）"}
    if state == "foreign":
        return {"action": "no_free_port", "port": port, "source": "pinned",
                "needs_restart": False,
                "reason": f"{port} 已被其它程序占用（{detail}），换一个端口"}
    return {"action": "pin", "port": port, "source": "pinned",
            "needs_restart": True, "reason": f"钉到 {port}（空闲）"}


def _conflict_of(cfg: dict | None, res: Resolution) -> str:
    """找出「本来想用、但被别人占着」的那个端口。"""
    act = res.facts.get("active_file") or read_active_port() or {}
    env = res.facts.get("env") if "env" in res.facts else env_port()
    for port in {int(act.get("port") or 0), int(env or 0), config_port(cfg)}:
        if not port:
            continue
        prb = probe(port, timeout=0.3)
        if prb["state"] == "foreign":
            return f"{prb['detail']}"
        info = listener(port)
        if info and not info["owned"]:
            return f"{info['cmd']} (pid {info['pid']})"
    return ""


# --------------------------------------------------------------------------
# 落盘（唯一会写文件的入口）
# --------------------------------------------------------------------------
def _rewrite_config_port(path: Path, port: int) -> bool:
    """文本级改 `cdp_port = N`，保留注释与其它字段。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    new, n = re.subn(r"(?m)^(\s*cdp_port\s*=\s*)(\d+)(\s*(?:#.*)?)$",
                     lambda m: f"{m.group(1)}{port}{m.group(3)}", text)
    if n == 0:
        return False
    if new != text:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(new, encoding="utf-8")
        os.replace(tmp, path)
    return True


def apply_port(cfg: dict | None, port: int, *,
               config_path: Path | None = None,
               setenv: bool = True,
               persist: bool = True) -> dict:
    """把端口落到三处：当前会话环境、开机 plist、config.toml。

    顺序有意义：setenv 立刻生效（本次登录内重启客户端就能用）；
    plist 保证重启电脑后仍然生效；config 只是回退值。
    """
    port = int(port)
    done: dict[str, Any] = {"port": port}
    if setenv:
        try:
            proc = subprocess.run(["launchctl", "setenv", ENV_KEY, str(port)],
                                  capture_output=True, text=True, timeout=6)
            done["setenv"] = proc.returncode == 0
            if proc.returncode != 0:
                done["setenv_error"] = (proc.stderr or "").strip()[:160]
        except (OSError, subprocess.SubprocessError) as exc:
            done["setenv"] = False
            done["setenv_error"] = str(exc)[:160]
    if persist:
        try:
            from . import launchd

            done["plist"] = launchd.install_cdp_env(port)
        except Exception as exc:  # noqa: BLE001 - 落盘失败不该中断主流程
            done["plist"] = {"ok": False, "error": str(exc)[:200]}
    path = Path(config_path) if config_path else _config_path(cfg)
    if path:
        done["config"] = _rewrite_config_port(path, port)
        done["config_path"] = str(path)
    if cfg is not None:
        cfg.setdefault("growth", {})["cdp_port"] = port
    write_cache(cfg, port, "applied")
    return done


def ensure_env(cfg: dict | None, port: int | None = None) -> dict:
    """让端口在当前登录会话 + 下次登录都成立。

    只能由 **launchd 托管进程**（daemon）调用：agent 自己的 shell 与宿主 App
    的进程没有 gui 域写权限，实测 `launchctl setenv` 会直接返回
    "Not privileged to set domain environment."，而 launchd job 里可以。

    做两件事：
      1) 写 cdp-env plist —— 文件放进 ~/Library/LaunchAgents 后，下次登录
         launchd 会自动加载并执行 setenv（不需要 bootstrap）。
      2) 当场 setenv —— 让本次登录内重启客户端也能拿到端口。
    """
    port = int(port or resolve(cfg).port)
    out: dict[str, Any] = {"port": port, "setenv": False, "plist": None}
    try:
        proc = subprocess.run(["launchctl", "setenv", ENV_KEY, str(port)],
                              capture_output=True, text=True, timeout=6)
        out["setenv"] = proc.returncode == 0
        if proc.returncode != 0:
            out["setenv_error"] = (proc.stderr or proc.stdout or "").strip()[:160]
    except (OSError, subprocess.SubprocessError) as exc:
        out["setenv_error"] = str(exc)[:160]
    try:
        from . import launchd

        out["plist"] = launchd.install_cdp_env(port, do_bootstrap=False)
    except Exception as exc:  # noqa: BLE001
        out["plist_error"] = str(exc)[:200]
    return out


def _config_path(cfg: dict | None) -> Path | None:
    raw = (cfg or {}).get("_config_path")
    if raw:
        return Path(str(raw))
    try:
        from . import config as config_mod

        return config_mod.DEFAULT_CONFIG_PATH
    except Exception:  # noqa: BLE001
        return None
