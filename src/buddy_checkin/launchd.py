"""LaunchAgent 渲染与加载。

两条独立链路：

  - `com.local.buddy-web-checkin`：常驻 daemon（run.sh daemon）
  - `com.local.workbuddy-cdp-env`：登录时 `launchctl setenv` 写入调试端口，
    保证「重启电脑后端口还在」。缺了它，手动 setenv 只在这一登录会话里有效。

放在独立模块里是为了让 `cli.py` 与 `ports.py` 都能用，而不互相 import 成环。

关于 `Bootstrap failed: 5: Input/output error`（2026-11-14 另一台机器实测）：
最常见是「同 label 已经在域里」，先 `bootout` 再 `bootstrap` 即可；
其次是 plist 的所有者/权限不对（必须是当前用户、不可组可写）。
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from typing import Any

DAEMON_LABEL = "com.local.buddy-web-checkin"
CDP_ENV_LABEL = "com.local.workbuddy-cdp-env"
ENV_KEY = "WORKBUDDY_REMOTE_DEBUGGING_PORT"


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def daemon_plist_path() -> Path:
    return launch_agents_dir() / f"{DAEMON_LABEL}.plist"


def cdp_env_plist_path() -> Path:
    return launch_agents_dir() / f"{CDP_ENV_LABEL}.plist"


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
def render_daemon(repo_root: str | Path, log_path: str | Path,
                  free_model: bool = True) -> str:
    script = Path(repo_root) / "run.sh"
    payload: dict[str, Any] = {
        "Label": DAEMON_LABEL,
        "ProgramArguments": ["/bin/sh", str(script), "daemon"],
        "WorkingDirectory": str(repo_root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    if free_model:
        payload["EnvironmentVariables"] = {"BUDDY_FREE_MODEL": "1"}
    return plistlib.dumps(payload, sort_keys=False).decode("utf-8")


def render_cdp_env(port: int) -> str:
    payload = {
        "Label": CDP_ENV_LABEL,
        "ProgramArguments": [
            "/bin/zsh", "-c", f"launchctl setenv {ENV_KEY} {int(port)}",
        ],
        "RunAtLoad": True,
    }
    return plistlib.dumps(payload, sort_keys=False).decode("utf-8")


def write_plist(path: Path, content: str) -> dict:
    """写 plist 并修正权限：launchd 不接受组/其他可写的文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass
    st = path.stat()
    return {"path": str(path), "mode": oct(st.st_mode & 0o777), "uid": st.st_uid}


# --------------------------------------------------------------------------
# 加载状态
# --------------------------------------------------------------------------
def _print_service(label: str) -> str:
    try:
        proc = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def loaded(label: str) -> bool:
    return bool(_print_service(label))


def service_state(label: str) -> dict:
    out = _print_service(label)
    if not out:
        return {"label": label, "loaded": False}
    pid = None
    state = ""
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("pid = "):
            pid = line.split("=", 1)[1].strip()
        elif line.startswith("state = "):
            state = line.split("=", 1)[1].strip()
    return {"label": label, "loaded": True, "pid": pid, "state": state}


def bootout(label: str) -> dict:
    try:
        proc = subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)[:160]}
    return {"ok": proc.returncode == 0,
            "error": (proc.stderr or "").strip()[:160] if proc.returncode else ""}


def bootstrap(label: str, path: Path, *, retry: bool = True) -> dict:
    """bootstrap；遇到「已经加载」先 bootout 再重试一次。"""
    try:
        proc = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)[:200]}
    err = (proc.stderr or "").strip()
    if proc.returncode == 0:
        return {"ok": True}
    if retry and loaded(label):
        bootout(label)
        return bootstrap(label, path, retry=False)
    hint = ""
    if "5:" in err or "Input/output error" in err:
        hint = ("（5 号错误在 2026-11-14 实测有三种成因：同 label 已加载、"
                "label 处于 disabled、或当前进程没有 gui 域写权限 —— "
                "从 agent/沙箱进程调用必然失败，请在终端里手动执行，"
                "或直接依赖「plist 放进 LaunchAgents 后下次登录自动加载」）")
    return {"ok": False, "error": f"{err[:200]}{hint}"}


# --------------------------------------------------------------------------
# 安装
# --------------------------------------------------------------------------
def install_cdp_env(port: int, *, do_bootstrap: bool = False) -> dict:
    """写 cdp-env plist（端口离开这个文件就活不过一次重启）。

    默认只写不 bootstrap：`~/Library/LaunchAgents` 下的 plist 会在**下次登录**
    被 launchd 自动加载并执行 setenv，不需要 bootstrap；而 bootstrap 需要 gui 域
    写权限，从 agent/沙箱进程调用必然是 5 号错误（2026-11-14 实测）。
    """
    path = cdp_env_plist_path()
    info = write_plist(path, render_cdp_env(port))
    result: dict[str, Any] = {"ok": True, "port": int(port), **info}
    if do_bootstrap:
        res = bootstrap(CDP_ENV_LABEL, path)
        result["bootstrap"] = res
        result["ok"] = bool(res.get("ok")) or loaded(CDP_ENV_LABEL)
        if not res.get("ok"):
            result["error"] = res.get("error", "")
    return result


def install_daemon(repo_root: str | Path, log_path: str | Path, *,
                   do_bootstrap: bool = False) -> dict:
    path = daemon_plist_path()
    info = write_plist(path, render_daemon(repo_root, log_path))
    result: dict[str, Any] = {"ok": True, **info}
    if do_bootstrap:
        res = bootstrap(DAEMON_LABEL, path)
        result["bootstrap"] = res
        result["ok"] = bool(res.get("ok"))
    return result
