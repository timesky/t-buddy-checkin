"""成长中心适配器（CDP 版）：直连 WorkBuddy 客户端内嵌 iframe，DOM 级操作。

取代旧的 CuaDriver/AX 通道（GrowthCenter）。原理（2026-11-12 实测）：

  - WorkBuddy 客户端是 Electron。设置环境变量 WORKBUDDY_REMOTE_DEBUGGING_PORT
    后主进程会 appendSwitch('remote-debugging-port')，暴露 CDP。
  - 成长中心是 https://www.workbuddy.cn/profile/growth-center，以 **OOPIF**
    （type=iframe）出现在 /json/list 里，可直连 WebSocket 执行 Runtime.evaluate。
  - 打开入口（侧栏「成长计划」→「立即前往」）不在主 renderer 的 DOM 里，
    只能靠 agent-browser 的跨 webContents 聚合 snapshot/click 触发。
  - 页面打开后，所有读取与点击都在 iframe 内用 JS 完成，比 AX 快且稳。

签到/补签的分工（2026-11-14 更正，5.11.3 实测）：
    当日签到只在主窗口左下角「Buddy加油站」浮层（见 adapters/fuel_card.py），
    **不在成长中心**。本适配器里的 checkin_status() 只是「11月活跃地图」读取，
    代表今天有没有用过客户端，与签到领积分无关，绝不能当签到判据
    （2026-11-13~14 就是按它判 confirmed=True，导致签到一次都没真正执行）。
    漏签只能在本页日历用补登卡补，不自动消耗。
    日历事实：.gs-cal-cell.is-today 今日格子，实心绿 rgb(111,229,198)=今天有记录；
    .gs-cal-cell.is-broken 漏签（可补登）。
"""
from __future__ import annotations

import contextlib
import functools
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .base import ActionResult
from ..timeutil import day_key

ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger(__name__)

# 页面状态判定用的按钮文案（与旧 AX 版保持一致，避免行为漂移）
COUNTDOWN_RE = re.compile(r"(\d{1,2}):([0-5]\d):([0-5]\d)")

# 兑换弹窗里「本期已兑完」的确定性文案
EXCHANGE_NO_MORE_MARKERS = (
    "本月已兑换", "本月已兑", "已兑换", "本月已领", "已兑完",
    "本轮已兑", "已达上限", "兑换完毕", "暂不可兑", "次数已用完",
)
# 派遣次数用尽
DISPATCH_EXHAUSTED_MARKERS = (
    "今日已派", "次数已用完", "次数用尽", "今日次数", "已达上限",
    "明日再来", "明天再来", "派遣次数", "已派完", "无可派遣",
)
# 弹层关闭按钮（精确匹配，避免误点资源类按钮）
DIALOG_CLOSE_LABELS = ("关闭", "×", "✕", "X", "我知道了", "知道了", "好的", "收到")
# 内部子页的「返回」：5.11.2 的兑换子页（.gs-embed-subpage）右上角是有 SVG 的
# <button class="gs-embed-back" aria-label="返回">，textContent 为空，按文案点不到。
# 实测结构（2026-11-12）：
#   主页面 subpage=0 back=0 close=1     <- close 是「关闭整个成长中心」
#   兑换子页 subpage=1 back=1 close=2   <- 多出来的 back 才是「回到上一层」
# 所以收内部弹层只许点 back；点 close 会把整个成长中心一起关掉（会打断同一轮
# 后续的派遣，表现为 state=unknown）。
DIALOG_BACK_SELECTORS = (".gs-embed-back", '[aria-label="返回"]')
# 整个成长中心弹窗的关闭按钮（只在 close_page 里用）
PAGE_CLOSE_SELECTORS = (".gs-embed-close", 'button[aria-label="关闭"]', 'button[title="关闭"]')

# 兑换弹层里**不是**里程碑奖励的文案。2026-11-12 实测：弹层打开与否，主面板
# 常驻按钮（领取礼物 / 抽奖 / 盲盒 / 派猫猫旅行）都留在 DOM 且 enabled，
# 只按「含 领取|兑换」筛选会把「领取礼物」当成奖励点掉（误触发旅行领奖）。
EXCHANGE_NOT_REWARD = (
    "礼物", "抽奖", "盲盒", "派猫猫", "旅行", "兑换奖励",
    "关闭", "取消", "补签",
    "还差", "未解锁", "已领取", "已兑换", "已兑完", "本月已兑", "本月已领",
    "已达上限", "兑换完毕", "暂不可兑",
)


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


def is_exchange_reward(label: str) -> bool:
    """按钮文案是否「兑换弹层里的里程碑奖励项」。

    2026-11-12 实测弹层（5.11.2）：里程碑奖励按钮文案就是**纯两字「兑换」**，
    已兑过的是 disabled + 文案「已兑换」；主面板的「领取礼物」在弹层打开时
    仍留在 DOM 且 enabled。因此：

      「兑换」        -> True   （真奖励项，别漏）
      「已兑换」      -> False  （已领完，且 disabled）
      「领取礼物」    -> False  （旅行线资源，误点会打乱 travel 时间线）

    保守原则：宁可漏点（返回 False，本轮 skipped），绝不误点面板按钮。
    """
    t = _norm(label)
    if not t or t in ("领取", "兑换奖励"):
        return False
    if any(k in t for k in EXCHANGE_NOT_REWARD):
        return False
    if t == "兑换":
        return True
    return any(k in t for k in ("领取", "兑换"))


def _auto_close(method: Callable) -> Callable:
    """任务级收尾：最外层方法结束后关掉本次自己打开的弹窗。

    嵌套调用（tick → claim_gift/dispatch）只在最外层收一次，不会中途把关掉的
    页面又打开一遍。用户在页面里自己操作时（页面本来就开着）只收内部弹层。
    """
    @functools.wraps(method)
    def wrapper(self: "GrowthBrowser", *args: Any, **kwargs: Any) -> Any:
        depth = getattr(self, "_depth", 0)
        self._depth = depth + 1
        if depth == 0:
            self._opened_this_run = False
        try:
            return method(self, *args, **kwargs)
        finally:
            self._depth = depth
            if depth == 0:
                self._wrap_up_session()

    return wrapper


class GrowthBrowser:
    """用 CDP 操作成长中心：读状态、领礼物、兑换、派遣。"""

    def __init__(self, cfg: dict, state: Any = None) -> None:
        g = cfg.get("growth", {}) or {}
        self.cfg = cfg
        self.state = state
        self.port = int(g.get("cdp_port", 9222))
        self.offset_sec = float(g.get("claim_offset_sec", 3))
        self.scene = (g.get("scene") or "").strip()
        self.wait_sec = float(g.get("open_wait_sec", 6))
        self.ab_bin = self._resolve_agent_browser(g)
        self.node_bin = self._resolve_node()
        self.cdp_js = ROOT / "tools" / "cdp_client.mjs"
        self.cdp_input = ROOT / "tools" / "cdp_input.mjs"
        self.block: tuple[str, str] | None = None
        self.last_close_ok: bool = False
        self._owner_ok: bool = False
        self._stale_hits: int = 0
        self._last_open_errors: list[str] = []
        self._last_open_clicked: bool = False
        self._depth: int = 0
        self._opened_this_run: bool = False

    # ---- 依赖定位 -----------------------------------------------------------
    @staticmethod
    def _resolve_agent_browser(g: dict) -> str | None:
        cand = g.get("agent_browser_bin") or ""
        paths = []
        if cand:
            paths.append(Path(cand).expanduser())
        paths.append(ROOT / "tools" / "node_modules" / ".bin" / "agent-browser")
        for p in paths:
            if p.exists():
                return str(p)
        return shutil.which("agent-browser")

    @staticmethod
    def _resolve_node() -> str | None:
        found = shutil.which("node")
        if found:
            return found
        base = Path.home() / ".workbuddy" / "binaries" / "node" / "versions"
        if base.exists():
            for p in sorted(base.glob("*/bin/node"), reverse=True):
                if p.exists():
                    return str(p)
        return None

    # ---- CDP 基础 -----------------------------------------------------------
    def _http(self, path: str) -> Any:
        import urllib.request

        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _version(self) -> dict:
        try:
            data = self._http("/json/version")
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def port_owner(self, targets: list[dict] | None = None) -> tuple[bool, str]:
        """判定 cdp_port 上跑的是不是 WorkBuddy 客户端。

        9222 是 Chrome 的默认调试端口，被别的浏览器/Electron 应用占用时，
        旧实现会当成「CDP 可达」，随后把 agent-browser 连到错误的浏览器上，
        报错只剩一句 "iframe never appeared"（2026-11-12 用假 CDP 复现）。
        返回 (是否 WorkBuddy, 归属描述)。
        """
        ver = self._version()
        ua = str(ver.get("User-Agent") or "")
        low = ua.lower()
        if "workbuddy" in low:
            return True, f"WorkBuddy ({ua[:60]})"
        for t in targets or []:
            blob = f"{t.get('url') or ''} {t.get('title') or ''}".strip()
            if "workbuddy" in blob.lower():
                return True, f"WorkBuddy ({ua[:60] or blob[:60]})"
        if not ua:
            # 拿不到 UA（极老版本/响应异常）时不新增拦截，保持原有行为
            return True, "unknown (no /json/version user-agent)"
        browser = str(ver.get("Browser") or "unknown browser")
        if "electron" not in low:
            return False, f"{browser}, not Electron ({ua[:70]})"
        return False, f"other Electron app ({ua[:70]})"

    def targets(self) -> list[dict]:
        try:
            data = self._http("/json/list")
        except Exception as exc:
            self.block = ("cdp_unavailable", f"CDP {self.port} unreachable: {exc}")
            return []
        lst = data if isinstance(data, list) else []
        if not lst:
            # 端口活着但暂无 target（客户端刚启动/窗口未建），不算错误
            if self.block and self.block[0] == "cdp_unavailable":
                self.block = None
            return lst
        if not self._owner_ok:
            owned, who = self.port_owner(lst)
            if not owned:
                self.block = (
                    "cdp_port_foreign",
                    f"port {self.port} is not WorkBuddy: {who}; "
                    f"确认 [growth].cdp_port 与 WORKBUDDY_REMOTE_DEBUGGING_PORT 一致，"
                    f"或换一个没被浏览器占用的端口",
                )
                return []
            self._owner_ok = True
        return lst

    def available(self) -> bool:
        return bool(self.targets())

    def _iframe_ws(self) -> str | None:
        for t in self.targets():
            if t.get("type") != "iframe":
                continue
            url = t.get("url") or ""
            if "growth-center" in url or "workbuddy.cn" in url:
                return t.get("webSocketDebuggerUrl")
        return None

    def _eval(self, js: str, timeout: int = 20) -> tuple[bool, Any]:
        """在成长中心 iframe 内执行 JS，返回 (ok, value)。"""
        ws = self._iframe_ws()
        if not ws:
            return False, "growth-center iframe not found"
        if not self.node_bin or not self.cdp_js.exists():
            return False, "node / cdp_client.mjs unavailable"
        try:
            proc = subprocess.run(
                [self.node_bin, str(self.cdp_js), ws, js, str(timeout * 1000)],
                capture_output=True, text=True, timeout=timeout + 5,
            )
        except subprocess.TimeoutExpired:
            return False, "cdp eval timeout"
        raw = (proc.stdout or "").strip()
        if not raw:
            return False, (proc.stderr or "empty output")[:300]
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return False, raw[:300]
        if not payload.get("ok"):
            return False, str(payload.get("error"))[:300]
        val = payload.get("value")
        if isinstance(val, str):
            try:
                return True, json.loads(val)
            except json.JSONDecodeError:
                return True, val
        return True, val

    # ---- agent-browser（打开页面） -----------------------------------------
    def _ab_env(self) -> dict[str, str]:
        """给 agent-browser 补 PATH。

        2026-11-12 实测：tools/node_modules/.bin/agent-browser 的 shebang 是
        ``#!/usr/bin/env node``。launchd 启动 daemon 时 PATH 只有
        /usr/bin:/bin:/usr/sbin:/sbin，找不到 node，每轮都报
        ``agent-browser connect failed``（手动 shell 里却有 node，所以之前没暴露）。
        """
        env = dict(os.environ)
        path = env.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin"
        extra: list[str] = []
        if self.node_bin:
            extra.append(str(Path(self.node_bin).parent))
        for p in ("/opt/homebrew/bin", "/usr/local/bin"):
            if p not in path and p not in extra:
                extra.append(p)
        env["PATH"] = ":".join([p for p in extra if p] + [path])
        return env

    def _ab(self, *args: str, timeout: int = 60) -> tuple[int, str]:
        if not self.ab_bin:
            return 127, "agent-browser not found"
        try:
            proc = subprocess.run(
                [self.ab_bin, *args], capture_output=True, text=True, timeout=timeout,
                env=self._ab_env(),
            )
        except subprocess.TimeoutExpired:
            return 124, "agent-browser timeout"
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def _ensure_connected(self, attempts: int = 2) -> bool:
        """复用已有 tab，必要时重新 connect。

        2026-11-12 实测：会话被其它 agent-browser 进程抢占时 `tab` 会瞬时失败，
        重试一次即可恢复，不必把整轮任务判成失败。
        """
        for i in range(attempts):
            code, out = self._ab("tab", timeout=30)
            if code == 0 and "t1" in out:
                return True
            code, _ = self._ab("connect", str(self.port), timeout=60)
            if code == 0:
                return True
            if i + 1 < attempts:
                time.sleep(1.5)
        return False

    def _click_label(self, label: str, attempts: int = 3) -> tuple[bool, str]:
        """在聚合快照里按按钮文案找 ref 并点击（可跨 webContents）。

        注意 snapshot 输出格式是 ``[ref=e478]``（不带 @），而 click 参数要 ``@e478``，
        2026-11-12 曾因此一直报 "entry not found"。
        """
        pattern = re.compile(r'button\s+"' + re.escape(label) + r'"')
        ref_pat = re.compile(r"\[ref=(e\d+)\]")
        last_err = ""
        for _ in range(attempts):
            code, out = self._ab("snapshot", "-i", timeout=60)
            if code != 0:
                last_err = f"snapshot rc={code}"
                time.sleep(1)
                continue
            ref = None
            for line in out.splitlines():
                if pattern.search(line):
                    m = ref_pat.search(line)
                    if m:
                        ref = "@" + m.group(1)
                        break
            if ref:
                code, res = self._ab("click", ref, timeout=60)
                if code == 0:
                    return True, label
                last_err = (res or "").strip()[:200] or f"click rc={code}"
                time.sleep(0.8)
            else:
                last_err = f'snapshot has no button "{label}"'
                time.sleep(1.2)
        return False, f'entry "{label}" failed: {last_err}'

    READY_JS = (
        "JSON.stringify({ready: document.readyState,"
        " btns: document.querySelectorAll('button').length,"
        " cal: document.querySelectorAll('.gs-cal-cell').length})"
    )

    def _wait_page_ready(self, timeout: float = 10.0) -> bool:
        """等 iframe 里的按钮与日历渲染完，避免刚打开就读到空页面。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            ok, val = self._eval(self.READY_JS, timeout=10)
            if ok and isinstance(val, dict):
                if val.get("btns", 0) > 0 and val.get("cal", 0) > 0:
                    return True
            time.sleep(0.6)
        return False

    def _open_via_labels(self, labels: list[str]) -> ActionResult | None:
        """依次点入口文案打开成长中心；全部失败返回 None 并记下原因。"""
        fails: list[str] = []
        clicked_any = False
        for label in labels:
            ok, detail = self._click_label(label)
            if not ok:
                fails.append(f"{label}: {detail}")
                continue
            clicked_any = True
            time.sleep(1.0)
            # 点入口后会出现「立即前往」
            ok2, _ = self._click_label("立即前往", attempts=2)
            if not ok2:
                # 有些版本入口直接打开页面，无需二次点击
                time.sleep(1.5)
            for _ in range(int(self.wait_sec * 2)):
                if self._iframe_ws():
                    ready = self._wait_page_ready()
                    # 本次亲手打开的页面，任务结束后要关掉（不把弹窗留给用户）
                    self._opened_this_run = True
                    return ActionResult(
                        True, "growth_open", f"opened via {label}",
                        {"opened": True, "entry": label, "ready": ready},
                    )
                time.sleep(0.5)
            fails.append(f"{label}: clicked but iframe never appeared")
        self._last_open_errors = fails
        self._last_open_clicked = clicked_any
        return None

    def _looks_like_client(self) -> bool:
        """快照里能否看到 WorkBuddy 客户端窗口。"""
        code, out = self._ab("snapshot", "-i", timeout=45)
        return code == 0 and "workbuddy" in out.lower()

    def _maybe_self_heal(self) -> None:
        """连续多轮连客户端窗口都看不到，判定 agent-browser 会话坏了，退出进程。

        2026-11-14 事故：客户端从 5.11.2 静默升级到 5.11.3 并重启，daemon 里
        长驻的 agent-browser 会话仍指向旧窗口，之后 8 小时每轮都报
        "no growth entry found in client"，自己不会好；手动重启进程立刻恢复。
        daemon 由 launchd KeepAlive 托管，退出后秒级拉起即重建会话。
        """
        if self._looks_like_client():
            self._stale_hits = 0
            return
        self._stale_hits += 1
        logger.warning(
            "agent-browser 看不到 WorkBuddy 窗口（连续 %d 次），疑似客户端升级/重启后"
            "会话失效", self._stale_hits,
        )
        if self._stale_hits >= 3:
            logger.error("会话连续失效 3 轮，退出进程交由 launchd 重建：%s",
                         self._last_open_errors)
            raise SystemExit(75)

    def ensure_page(self) -> ActionResult:
        """确保成长中心 iframe 存在且已渲染；不存在就从客户端入口打开。"""
        if self._iframe_ws():
            ready = self._wait_page_ready()
            return ActionResult(True, "growth_open", "page already open",
                                {"opened": False, "ready": ready})
        if not self.available():
            code, msg = self.block or ("cdp_unavailable", "")
            return ActionResult(False, "growth_open", msg, {"blocked": code})

        labels = ["成长计划"] + list((self.cfg.get("growth", {}) or {}).get("open_labels") or [])
        self._last_open_errors = []
        self._last_open_clicked = False
        for attempt in range(2):
            if not self._ensure_connected():
                return ActionResult(False, "growth_open", "agent-browser connect failed")
            res = self._open_via_labels(labels)
            if res is not None:
                self._stale_hits = 0
                return res
            if attempt == 0:
                # 客户端升级/重启后会话可能还指着旧窗口：重连一次再试
                self._ab("connect", str(self.port), timeout=60)

        self._maybe_self_heal()
        detail = ("opened entry but growth-center iframe never appeared"
                  if self._last_open_clicked
                  else "no growth entry found in client")
        if self._last_open_errors:
            detail += "（" + "; ".join(self._last_open_errors)[:240] + "）"
        return ActionResult(False, "growth_open", detail)

    # ---- 页面状态读取 -------------------------------------------------------
    READ_JS = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const btns = [...document.querySelectorAll('button')].map(b => ({
    t: norm(b.textContent), dis: !!b.disabled,
    cls: String(b.className || ''), eid: b.getAttribute('dt-eid') || '',
    aria: b.getAttribute('aria-label') || b.getAttribute('title') || ''
  }));
  const body = norm(document.body.innerText);
  const cd = btns.find(b => /旅行倒计时/.test(b.t)) || null;
  let state = 'unknown';
  if (cd) state = 'traveling';
  else if (btns.some(b => /领取/.test(b.t) && /积分/.test(b.t) && !/已领取/.test(b.t))) state = 'gift_dialog';
  else if (btns.some(b => b.t === '领取礼物')) state = 'claimable';
  else if (btns.some(b => /确定派出/.test(b.t))) state = 'scene_dialog';
  else if (btns.some(b => /派猫猫旅行/.test(b.t))) state = 'idle';
  let remaining = null;
  if (cd) {
    const m = cd.t.match(/(\d{1,2}):([0-5]\d):([0-5]\d)/);
    if (m) remaining = (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3]);
  }
  return JSON.stringify({state, remaining, buttons: btns.slice(0, 40), body: body.slice(0, 1500)});
})()
"""

    CHECKIN_JS = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const today = document.querySelector('.gs-cal-cell.is-today');
  const cells = [...document.querySelectorAll('.gs-cal-cell')];
  let checked = false, bg = '', day_label = '';
  if (today) {
    bg = getComputedStyle(today).backgroundColor || '';
    checked = today.classList.contains('is-checked') || /111,\s*229,\s*198/.test(bg);
    day_label = norm(today.textContent || today.getAttribute('aria-label') || '');
  }
  // 跨零点假阳性守卫：刚过 0 点时页面可能还没切日，is-today 仍指向前一天。
  // 2026-11-13 00:00:51 就是这样把 11-12 的 streak=10d 当成了 11-13 已签到，
  // 导致当天不再检查、真漏签。
  // 实测（5.11.3）：今日格子的文本是「今日」而非日号，但它的**前一个兄弟格子**
  // 就是昨天，文本为日号（今天 14 号 → prev 是 "13"）。用 prev 与「今天-1」比对
  // 即可判断日历是否已经切日；1 号则比对 28~31（上月最后一天）。
  const prev = today ? today.previousElementSibling : null;
  const prev_txt = prev ? norm(prev.textContent) : '';
  const prev_num = /^\d{1,2}$/.test(prev_txt) ? parseInt(prev_txt, 10) : null;
  const expected = %d;
  let switched = true;
  if (today && prev_num !== null) {
    if (expected >= 2) switched = (prev_num === expected - 1);
    else switched = (prev_num >= 28 && prev_num <= 31);
  }
  const makeup = cells
    .filter(c => c.classList.contains('is-broken'))
    .map(c => norm(c.textContent));
  const m = norm(document.body.innerText).match(/当前连续登录\s*(\d+)\s*天/);
  return JSON.stringify({
    has_today: !!today, checked, bg, makeup, switched, day_label,
    streak: m ? parseInt(m[1], 10) : null,
    days: cells.length
  });
})()
"""

    def _checkin_js(self) -> str:
        """把北京时间的今天日号注入签到判定脚本（跨零点守卫用）。"""
        day = int(day_key().rsplit("-", 1)[-1])
        return self.CHECKIN_JS % day

    def read_state(self) -> dict:
        ok, val = self._eval(self.READ_JS)
        if not ok:
            return {"state": "unknown", "remaining_sec": None, "raw": str(val), "error": str(val)}
        info = val if isinstance(val, dict) else {}
        return {
            "state": info.get("state", "unknown"),
            "remaining_sec": info.get("remaining"),
            "raw": info.get("body", ""),
            "buttons": info.get("buttons", []),
        }

    def checkin_status(self) -> dict:
        """读活跃地图判定今天是否已打卡（签到）。"""
        ok, val = self._eval(self._checkin_js())
        if not ok:
            return {"ok": False, "detail": str(val)}
        info = val if isinstance(val, dict) else {}
        return {
            "ok": True,
            "checked": bool(info.get("checked")),
            "has_today": bool(info.get("has_today")),
            "switched": bool(info.get("switched", True)),
            "day_label": info.get("day_label") or "",
            "streak": info.get("streak"),
            "makeup": info.get("makeup") or [],
            "bg": info.get("bg"),
        }

    # ---- 点击辅助 -----------------------------------------------------------
    @staticmethod
    def _click_js(expr: str) -> str:
        """生成按文本匹配并 click 的 JS。expr 是 JS 布尔表达式，变量 t 为按钮文案。"""
        return (
            "(() => {"
            "  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();"
            "  const btns = [...document.querySelectorAll('button')];"
            "  for (const b of btns) {"
            "    const t = norm(b.textContent);"
            f"    if (({expr}) && !b.disabled) {{ b.click(); return JSON.stringify({{ok:true,label:t}}); }}"
            "  }"
            "  const d = btns.find(b => {" + f"const t = norm(b.textContent); return ({expr});" + "});"
            "  return JSON.stringify({ok:false, reason: d ? 'disabled' : 'not_found', label: d ? norm(d.textContent) : ''});"
            "})()"
        )

    def _click_by_expr(self, expr: str) -> tuple[bool, str]:
        ok, val = self._eval(self._click_js(expr))
        if not ok:
            return False, str(val)
        info = val if isinstance(val, dict) else {}
        if info.get("ok"):
            return True, str(info.get("label", ""))
        return False, f"{info.get('reason')}: {info.get('label')}"

    def _click_text(self, text: str) -> tuple[bool, str]:
        return self._click_by_expr(f"t === {json.dumps(text, ensure_ascii=False)}")

    def _click_selectors(self, selectors: tuple[str, ...]) -> tuple[bool, str]:
        """按 CSS 选择器点击第一个可用的（用于无文案的图标按钮）。"""
        js = (
            "(() => {"
            f"  const sels = {json.dumps(list(selectors))};"
            "  for (const s of sels) {"
            "    const el = document.querySelector(s);"
            "    if (el && !el.disabled) { el.click(); return JSON.stringify({ok:true, sel:s}); }"
            "  }"
            "  return JSON.stringify({ok:false, reason:'no_selector_hit'});"
            "})()"
        )
        ok, val = self._eval(js)
        if not ok:
            return False, str(val)
        info = val if isinstance(val, dict) else {}
        if info.get("ok"):
            return True, str(info.get("sel", ""))
        return False, str(info.get("reason", "no_selector_hit"))

    # ---- 真实输入（补签弹层只认 trusted click） -----------------------------
    def _input_click(self, x: int, y: int, timeout: int = 15) -> tuple[bool, str]:
        """在 iframe 视口坐标处发一次真实鼠标左键（CDP Input.dispatchMouseEvent）。"""
        ws = self._iframe_ws()
        if not ws:
            return False, "growth iframe not open"
        if not self.node_bin or not self.cdp_input.exists():
            return False, "node / cdp_input.mjs unavailable"
        try:
            proc = subprocess.run(
                [self.node_bin, str(self.cdp_input), ws, str(int(x)), str(int(y))],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False, "input dispatch timeout"
        raw = (proc.stdout or "").strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return False, (raw or proc.stderr or "no output")[:200]
        return bool(payload.get("ok")), str(payload.get("error") or "ok")

    # ---- 补签（成长中心日历，消耗补登卡） -----------------------------------
    MAKEUP_SCAN_JS = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const cells = [...document.querySelectorAll('.gs-cal-cell.is-makeup-clickable')];
  const items = cells.map(c => {
    let remark = {};
    try { remark = JSON.parse(c.getAttribute('dt-remark') || '{}'); } catch (e) {}
    return {
      date: remark.date || '',
      balance: (remark.cardBalance === undefined ? null : remark.cardBalance),
      label: norm(c.textContent)
    };
  });
  const text = norm(document.body.innerText);
  const m = text.match(/补[登签]卡\s*[×xX*]\s*(\d+)/);
  return JSON.stringify({
    items,
    card_text: m ? m[0] : '',
    card_balance: m ? parseInt(m[1], 10) : (items[0] && items[0].balance !== null ? items[0].balance : null)
  });
})()
"""

    def makeup_scan(self) -> dict:
        """读漏签格（可补登）与补登卡余量。

        实测（2026-11-14）：漏签格是
        <button class="gs-cal-cell is-broken is-makeup-clickable"
                dt-remark='{"date":"2026-11-01","cardBalance":2}'>1</button>
        卡片余量同时出现在按钮的 dt-remark.cardBalance 和页面「补登卡 ×2」文案里。
        """
        ok, val = self._eval(self.MAKEUP_SCAN_JS)
        if not ok:
            return {"ok": False, "detail": str(val), "items": [], "card_balance": None}
        info = val if isinstance(val, dict) else {}
        return {
            "ok": True,
            "items": info.get("items") or [],
            "card_balance": info.get("card_balance"),
            "card_text": info.get("card_text") or "",
        }

    def _makeup_cell_rect(self, date: str) -> tuple[bool, dict | str]:
        js = (
            "(() => {"
            "  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();"
            f"  const target = {json.dumps(str(date))};"
            "  const cells = [...document.querySelectorAll('.gs-cal-cell.is-makeup-clickable')];"
            "  const cell = cells.find(c => (c.getAttribute('dt-remark') || '').indexOf(target) >= 0);"
            "  if (!cell) return JSON.stringify({ok:false, reason:'cell not found'});"
            "  cell.scrollIntoView({block: 'center', behavior: 'instant'});"
            "  const r = cell.getBoundingClientRect();"
            "  return JSON.stringify({ok:true, cx: Math.round(r.left + r.width/2),"
            "    cy: Math.round(r.top + r.height/2), w: Math.round(r.width), h: Math.round(r.height),"
            "    vh: innerHeight, label: norm(cell.textContent)});"
            "})()")
        ok, val = self._eval(js)
        if not ok:
            return False, str(val)
        return True, val if isinstance(val, dict) else {}

    def _makeup_tip_rect(self) -> tuple[bool, dict | str]:
        js = (
            "(() => {"
            "  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();"
            "  const tip = document.querySelector('.gs-cell-toast--makeup');"
            "  if (!tip) return JSON.stringify({ok:false, reason:'makeup tip not shown'});"
            "  const btn = tip.querySelector('.gs-cell-toast-action');"
            "  if (!btn) return JSON.stringify({ok:false, reason:'use button missing'});"
            "  const r = btn.getBoundingClientRect();"
            "  return JSON.stringify({ok:true, cx: Math.round(r.left + r.width/2),"
            "    cy: Math.round(r.top + r.height/2), text: norm(tip.textContent),"
            "    btn: norm(btn.textContent)});"
            "})()")
        ok, val = self._eval(js)
        if not ok:
            return False, str(val)
        return True, val if isinstance(val, dict) else {}

    def _makeup_one(self, date: str) -> tuple[bool, str]:
        """补签某一天：点漏签格 → 点「使用」消耗一张补登卡 → 复核格子变化。"""
        ok, rect = self._makeup_cell_rect(date)
        if not ok or not isinstance(rect, dict) or not rect.get("ok"):
            return False, f"{date}: {rect}"
        cy = int(rect.get("cy") or 0)
        vh = int(rect.get("vh") or 0)
        if vh and (cy < 0 or cy > vh):
            return False, f"{date}: cell off-screen after scroll (cy={cy}, vh={vh})"

        ok2, why = self._input_click(int(rect["cx"]), cy)
        if not ok2:
            return False, f"{date}: click cell failed ({why})"
        time.sleep(0.8)

        ok3, tip = self._makeup_tip_rect()
        if not ok3 or not isinstance(tip, dict) or not tip.get("ok"):
            return False, f"{date}: {tip}"
        if "补登" not in str(tip.get("btn", "")) and "使用" not in str(tip.get("btn", "")):
            return False, f"{date}: unexpected tip button {tip.get('btn')!r}"

        ok4, why4 = self._input_click(int(tip["cx"]), int(tip["cy"]))
        if not ok4:
            return False, f"{date}: click 使用 failed ({why4})"
        time.sleep(1.6)

        scan = self.makeup_scan()
        still = [i for i in scan.get("items") or [] if str(i.get("date")) == str(date)]
        if still:
            return False, f"{date}: still broken after 使用 (balance={scan.get('card_balance')})"
        return True, f"{date} 补签成功"

    @_auto_close
    def makeup(self, max_cards: int | None = None) -> ActionResult:
        """把当天能补的漏签都补掉（每补一天消耗一张补登卡）。

        返回 data["state"]：
          done         没有漏签了（补完或本来就没有）
          impossible   还有漏签但补登卡为 0，今天补不了
          pending      补签失败（页面没响应/点了没变化），稍后重试
        """
        opened = self.ensure_page()
        if not opened.ok:
            return ActionResult(False, "makeup", opened.detail,
                                {"state": "pending"})
        self._close_dialog()
        scan = self.makeup_scan()
        if not scan.get("ok"):
            return ActionResult(False, "makeup", f"scan failed: {scan.get('detail')}",
                                {"state": "pending"})
        items = scan.get("items") or []
        balance = scan.get("card_balance")
        if not items:
            return ActionResult(True, "makeup", "no missed day",
                                {"state": "done", "broken": [], "card_balance": balance})
        if balance == 0:
            return ActionResult(
                True, "makeup",
                f"no makeup card left (missed={','.join(str(i.get('date')) for i in items)})",
                {"state": "impossible", "broken": [i.get("date") for i in items],
                 "card_balance": balance})

        limit = len(items) if max_cards is None else min(len(items), int(max_cards))
        if balance is not None:
            limit = min(limit, int(balance))
        done_days: list[str] = []
        errors: list[str] = []
        for item in items[:limit]:
            date = str(item.get("date") or item.get("label") or "")
            if not date:
                continue
            ok, detail = self._makeup_one(date)
            if ok:
                done_days.append(date)
            else:
                errors.append(detail)

        after = self.makeup_scan()
        left = after.get("items") or []
        balance_after = after.get("card_balance")
        data = {
            "made_up": done_days,
            "errors": errors,
            "broken_left": [i.get("date") for i in left],
            "card_balance": balance_after,
        }
        if not left:
            data["state"] = "done"
            return ActionResult(True, "makeup",
                                f"made up {len(done_days)} day(s): {','.join(done_days) or '-'}",
                                data)
        if balance_after == 0:
            data["state"] = "impossible"
            return ActionResult(True, "makeup",
                                f"made up {len(done_days)}; no card left for "
                                f"{','.join(str(i.get('date')) for i in left)}", data)
        data["state"] = "pending"
        return ActionResult(
            False, "makeup",
            f"made up {len(done_days)}; still broken "
            f"{','.join(str(i.get('date')) for i in left)}; errors={'; '.join(errors)[:200]}",
            data)

    # ---- 抽奖 ---------------------------------------------------------------
    def lottery_tickets(self, st: dict | None = None) -> tuple[int, str]:
        """读抽奖券数（按钮「抽奖(N)」）。返回 (N, 原始文案)。"""
        state = st if st is not None else self.read_state()
        for b in state.get("buttons", []):
            label = _norm(b.get("t", ""))
            m = re.search(r"抽奖\s*\((\d+)\)", label)
            if m:
                return int(m.group(1)), label
        return 0, ""

    @_auto_close
    def lottery(self, max_draws: int = 10) -> ActionResult:
        """把今天的抽奖券抽掉。

        实测流程（2026-11-14，5.11.3）：
          主面板按钮「抽奖(N)」 → 点开**子页**（不是弹层）
          子页文案「福利抽奖 每日任务轮盘 … 当前剩余 N 次」
          子页按钮 `button.gs-wheel-center`（文案「START抽奖」）→ 转盘
          抽完显示「积分已发放，将在几分钟内到账 / 抽奖卡已用完 / 剩余抽奖卡 0」
        所以「没抽到弹层」不能算失败，必须按子页结构走。

        返回 data["state"]：done（券用完）/ impossible（本来就没券）/
        pending（点了没反应，稍后重试）。
        """
        opened = self.ensure_page()
        if not opened.ok:
            return ActionResult(False, "lottery", opened.detail, {"state": "pending"})
        self._close_dialog()
        tickets, label = self.lottery_tickets()
        if tickets <= 0:
            return ActionResult(True, "lottery", "no lottery ticket",
                                {"state": "done", "tickets": 0})

        ok, clicked = self._click_text(label)
        if not ok:
            return ActionResult(False, "lottery", f"open lottery page failed: {clicked}",
                                {"state": "pending", "tickets": tickets})
        time.sleep(1.6)

        draws = 0
        errors: list[str] = []
        for _ in range(min(tickets, max_draws)):
            ok2, how = self._click_selectors((".gs-wheel-center",))
            if not ok2:
                errors.append(f"wheel button not found: {how}")
                break
            left = self._wait_draw_result(timeout=12.0)
            draws += 1
            if left is None:
                errors.append("draw result not readable")
                break
            if left <= 0:
                break

        self._close_dialog()
        time.sleep(0.5)
        left_tickets, left_label = self.lottery_tickets()
        data = {"draws": draws, "tickets_left": left_tickets,
                "errors": errors, "label": left_label}
        if left_tickets <= 0:
            data["state"] = "done"
            return ActionResult(True, "lottery",
                                f"drew {draws} time(s), no ticket left", data)
        data["state"] = "pending"
        return ActionResult(
            False, "lottery",
            f"drew {draws}, {left_tickets} ticket(s) left; {('; '.join(errors))[:200]}",
            data)

    WHEEL_LEFT_JS = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const sub = document.querySelector('.gs-embed-subpage') || document.body;
  const t = norm(sub.innerText);
  let left = null;
  let m = t.match(/当前剩余\s*(\d+)\s*次/);
  if (m) left = parseInt(m[1], 10);
  if (left === null) {
    m = t.match(/剩余抽奖卡\s*(\d+)/);
    if (m) left = parseInt(m[1], 10);
  }
  const done = /抽奖卡已用完|抽奖次数已用完|已抽完/.test(t);
  return JSON.stringify({
    left: left, done: done, spinning: /转动中/.test(t),
    text: t.slice(0, 200)
  });
})()
"""

    def _wait_draw_result(self, timeout: float = 12.0) -> int | None:
        """等转盘转完，返回剩余抽奖券数；读不到返回 None。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.9)
            ok, val = self._eval(self.WHEEL_LEFT_JS)
            info = val if isinstance(val, dict) else {}
            if not info:
                continue
            if info.get("done"):
                return 0
            left = info.get("left")
            if isinstance(left, int) and not info.get("spinning"):
                return left
        return None

    def _close_dialog(self) -> bool:
        """收掉内部子页/弹层，回到成长中心主页面。

        只点「返回」（有文案的关闭按钮也兼容）；**绝不点整页的关闭图标**，
        否则会把成长中心整个关掉，打断同一轮后续动作。
        """
        labels = ", ".join(json.dumps(x, ensure_ascii=False) for x in DIALOG_CLOSE_LABELS)
        expr = f"[{labels}].includes(t)"
        ok, _ = self._click_by_expr(expr)
        if not ok:
            ok, _ = self._click_selectors(DIALOG_BACK_SELECTORS)
        if ok:
            time.sleep(0.4)
            return True
        return False

    # ---- 收尾（关闭本次打开的弹窗） -----------------------------------------
    def close_page(self, attempts: int = 2) -> bool:
        """关闭成长中心整个弹窗，返回是否已关闭。

        只在「本次由工具打开」时由 _wrap_up 调用：用户自己开着的页面不碰，
        避免和人抢界面。先用「返回」退回主页面，再点整页关闭图标；
        都失败才按 ESC 兜底（ESC 是真实按键，会抢焦点，所以放最后）。
        """
        for _ in range(attempts):
            if not self._iframe_ws():
                self.last_close_ok = True
                return True
            self._close_dialog()                      # 先退出子页，避免遮挡关闭按钮
            if not self._iframe_ws():
                self.last_close_ok = True
                return True
            ok, _ = self._click_selectors(PAGE_CLOSE_SELECTORS)
            if not ok:
                ok, _ = self._click_text("关闭")
            if not ok:
                self._ab("press", "Escape", timeout=30)
            time.sleep(0.6)
        closed = not self._iframe_ws()
        self.last_close_ok = closed
        return closed

    def _wrap_up_session(self) -> None:
        """任务收尾：内部弹层一律收掉；整个弹窗只在本次自己打开时关闭。"""
        try:
            self._close_dialog()
        except Exception:
            pass
        if not getattr(self, "_opened_this_run", False):
            self.last_close_ok = False
            return
        if not bool((self.cfg.get("growth", {}) or {}).get("close_page_after_action", True)):
            return
        self.close_page()

    # ---- 批量会话（一次打开跑多个任务） ------------------------------------
    @contextlib.contextmanager
    def session(self):
        """把多个任务合并到同一次打开里（补签/兑换/抽奖共用一页）。

        否则每个任务各自打开→关闭，一轮要开关三四次窗口，又慢又打扰。
        进入时 depth+1，内部方法的 _auto_close 就不会收尾；退出时统一收一次。
        """
        self._depth = getattr(self, "_depth", 0) + 1
        if self._depth == 1:
            self._opened_this_run = False
        try:
            yield self
        finally:
            self._depth -= 1
            if self._depth == 0:
                self._wrap_up_session()

    # ---- 业务动作 -----------------------------------------------------------
    @_auto_close
    def claim_gift(self) -> ActionResult:
        opened = self.ensure_page()
        if not opened.ok:
            return ActionResult(False, "gift", opened.detail)
        st = self.read_state()
        state = st["state"]

        if state == "gift_dialog":
            ok, label = self._click_by_expr("/领取/.test(t) && /积分/.test(t)")
            if not ok:
                self._close_dialog()
                return ActionResult(False, "gift", label)
            time.sleep(1.2)
            after = self.read_state()
            confirmed = "已领取" in after["raw"] or after["state"] in ("idle", "traveling")
            self._close_dialog()
            return ActionResult(confirmed, "gift", f"claimed {label}",
                                {"confirmed": confirmed})

        if state != "claimable":
            return ActionResult(True, "gift", f"nothing claimable (state={state})",
                                {"state": state})

        ok, label = self._click_text("领取礼物")
        if not ok:
            self._close_dialog()
            return ActionResult(False, "gift", label)
        time.sleep(1.0)
        ok2, label2 = self._click_by_expr("/领取/.test(t) && /积分/.test(t)")
        if not ok2:
            self._close_dialog()
            return ActionResult(False, "gift", f"gift dialog: {label2}")
        time.sleep(1.2)
        after = self.read_state()
        confirmed = "已领取" in after["raw"] or after["state"] in ("idle", "traveling")
        self._close_dialog()
        return ActionResult(confirmed, "gift", f"claimed {label2}",
                            {"confirmed": confirmed, "label": label2})

    @staticmethod
    def scan_exchanges(st: dict) -> list[tuple[str, str]]:
        """扫描可兑项：里程碑、抽奖>0、能量满的盲盒。补签卡永不自动消耗。"""
        found: list[tuple[str, str]] = []
        for b in st.get("buttons", []):
            if b.get("dis"):
                continue
            label = _norm(b.get("t", ""))
            if label == "兑换奖励":
                found.append((label, "milestone"))
                continue
            m = re.search(r"抽奖\((\d+)\)", label)
            if m and int(m.group(1)) > 0:
                found.append((label, "lottery"))
                continue
            m = re.search(r"开启盲盒\s*(\d+)\s*/\s*(\d+)", label)
            if m and int(m.group(1)) >= int(m.group(2)):
                found.append((label, "blindbox"))
        return found

    @_auto_close
    def claim_exchanges(self, claimed_before: Callable[[str], bool] | None = None) -> list[ActionResult]:
        opened = self.ensure_page()
        if not opened.ok:
            return [ActionResult(False, "exchange", opened.detail)]
        st = self.read_state()
        if "选择奖励" in st["raw"]:
            self._close_dialog()
            st = self.read_state()

        candidates = self.scan_exchanges(st)
        if not candidates:
            self._close_dialog()
            return [ActionResult(True, "exchange", "nothing exchangeable", {"count": 0})]

        results: list[ActionResult] = []
        for label, kind in candidates:
            key = f"{kind}:{label}"
            if claimed_before is not None and claimed_before(key):
                results.append(ActionResult(
                    True, "exchange", f"already claimed today: {key}",
                    {"kind": kind, "label": label, "key": key, "skipped": True}))
                continue
            ok, clicked = self._click_text(label)
            if not ok:
                results.append(ActionResult(False, "exchange", f"{kind}: {clicked}",
                                            {"kind": kind, "key": key}))
                continue
            time.sleep(1.2)
            after = self.read_state()

            if kind == "milestone":
                unlocked = [
                    _norm(b.get("t", "")) for b in after.get("buttons", [])
                    if not b.get("dis")
                    and is_exchange_reward(b.get("t", ""))
                    and "还差" not in str(b.get("aria") or "")
                    and "已兑换" not in str(b.get("aria") or "")
                ]
                if not unlocked:
                    self._close_dialog()
                    no_more = next((m for m in EXCHANGE_NO_MORE_MARKERS if m in after["raw"]), None)
                    results.append(ActionResult(
                        True, "exchange",
                        (f"exchange concluded: {no_more}" if no_more
                         else "milestone: no unlocked reward yet"),
                        {"kind": kind, "key": key, "skipped": True,
                         "conclusive": bool(no_more)}))
                    continue
                target = unlocked[0]
                ok2, click_label = self._click_text(target)
                time.sleep(1.4)
                # 兑换后必须复核：奖励项应变成「已兑换」或从可兑列表消失，
                # 只凭「点到了按钮」不算成功（否则账本会记成 done 而其实没兑）。
                ver = self.read_state()
                after_unlocked = [
                    _norm(b.get("t", "")) for b in ver.get("buttons", [])
                    if not b.get("dis")
                    and is_exchange_reward(b.get("t", ""))
                    and "还差" not in str(b.get("aria") or "")
                    and "已兑换" not in str(b.get("aria") or "")
                ]
                consumed = len(after_unlocked) < len(unlocked)
                self._close_dialog()
                if not ok2 and any(m in str(click_label) + target for m in EXCHANGE_NO_MORE_MARKERS):
                    results.append(ActionResult(
                        True, "exchange", f"exchange concluded: {target}",
                        {"kind": kind, "key": key, "skipped": True, "conclusive": True}))
                    continue
                results.append(ActionResult(
                    ok2 and consumed, "exchange",
                    f"milestone claimed: {target}" if consumed
                    else f"milestone click had no effect: {target}",
                    {"kind": kind, "key": key, "label": target, "consumed": consumed,
                     "unlocked_before": len(unlocked), "unlocked_after": len(after_unlocked)}))
                continue

            still = any(_norm(b.get("t", "")) == label for b in after.get("buttons", []))
            results.append(ActionResult(
                ok and not still, "exchange", f"{kind}: {clicked}",
                {"kind": kind, "label": label, "key": key, "changed": not still}))

        self._close_dialog()
        return results

    @_auto_close
    def dispatch(self) -> ActionResult:
        opened = self.ensure_page()
        if not opened.ok:
            return ActionResult(False, "travel_dispatch", opened.detail)
        self._close_dialog()
        st = self.read_state()
        state, raw = st["state"], st["raw"]

        hit = next((m for m in DISPATCH_EXHAUSTED_MARKERS if m in raw), None)
        if hit:
            return ActionResult(False, "travel_dispatch",
                                f"dispatch exhausted today (page shows {hit})",
                                {"dispatch_blocked": "exhausted", "evidence": hit})

        dispatched_now = False
        if state == "idle":
            ok, label = self._click_by_expr("/派猫猫旅行/.test(t)")
            if not ok:
                self._close_dialog()
                st2 = self.read_state()
                hit = next((m for m in DISPATCH_EXHAUSTED_MARKERS if m in st2["raw"]), None)
                return ActionResult(
                    False, "travel_dispatch", label,
                    {"dispatch_blocked": "exhausted" if hit else None})
            time.sleep(1.5)
            dispatched_now = True
            st = self.read_state()
            state = st["state"]

        if state == "scene_dialog":
            if self.scene:
                self._click_by_expr(
                    "/切换到/.test(t) && t.indexOf(" + json.dumps(self.scene, ensure_ascii=False) + ") >= 0")
                time.sleep(0.8)
            ok, label = self._click_by_expr("/确定派出/.test(t)")
            if not ok:
                return ActionResult(False, "travel_dispatch", label)

        # 派遣是异步的：等页面从 scene_dialog 变成 traveling
        info = self.read_state()
        for _ in range(3):
            if info["state"] == "traveling" and info["remaining_sec"] is not None:
                break
            time.sleep(1.2)
            info = self.read_state()

        if info["state"] != "traveling" or info["remaining_sec"] is None:
            self._close_dialog()
            hit = next((m for m in DISPATCH_EXHAUSTED_MARKERS if m in info["raw"]), None)
            return ActionResult(
                False, "travel_dispatch",
                f"dispatch not confirmed (state={info['state']})",
                {"state": info["state"],
                 "dispatch_blocked": "exhausted" if hit else None})

        due = time.time() + float(info["remaining_sec"]) + self.offset_sec
        self._close_dialog()
        return ActionResult(
            True, "travel_dispatch",
            (f"dispatched; real duration {info['remaining_sec']}s" if dispatched_now
             else f"already traveling; real duration {info['remaining_sec']}s"),
            {"remaining_sec": info["remaining_sec"], "due_at": due,
             "scene": self.scene or "default", "dispatched": dispatched_now})

    @_auto_close
    def tick(self, force: bool = False) -> ActionResult:
        """一轮：旅行中→记录真实到期；可领→领奖并再派遣；空闲→派遣。"""
        opened = self.ensure_page()
        if not opened.ok:
            return ActionResult(False, "travel", opened.detail)
        self._close_dialog()
        info = self.read_state()
        state = info["state"]

        if state == "traveling" and not force:
            if info["remaining_sec"] is None:
                return ActionResult(False, "travel", "countdown unreadable", info)
            due = time.time() + float(info["remaining_sec"]) + self.offset_sec
            return ActionResult(
                True, "travel",
                f"traveling; due from real countdown ({info['remaining_sec']}s)",
                {"state": state, "due_at": due, "remaining_sec": info["remaining_sec"]})

        # 不在旅行中（idle / claimable / gift_dialog / 未知）：先领奖，再派遣。
        # 绝不能只派遣 —— 倒计时走完后页面回到 idle，「领取礼物」还在，
        # 只派遣会把这次的奖励漏掉（2026-11-14 修正）。
        claimed = self.claim_gift()
        dispatched = self.dispatch()
        data: dict[str, Any] = {
            "state": state, "due_at": dispatched.data.get("due_at"),
            "claim": claimed.as_dict(), "dispatch": dispatched.as_dict(),
        }
        if dispatched.data.get("dispatch_blocked"):
            data["dispatch_blocked"] = dispatched.data["dispatch_blocked"]
        ok = dispatched.ok and (claimed.ok or claimed.data.get("state") == "traveling")
        return ActionResult(ok, "travel",
                            f"claim={claimed.detail}; dispatch={dispatched.detail}", data)

    # ---- 签到（状态校验） ---------------------------------------------------
    @_auto_close
    def checkin(self) -> ActionResult:
        """读「11月活跃地图」：今天有没有留下活跃记录。

        **这不是签到判据**（2026-11-14 更正）：签到在左下角浮层（FuelCard），
        活跃地图只代表今天用过客户端。保留本方法仅供诊断与漏签排查。
        """
        opened = self.ensure_page()
        if not opened.ok:
            return ActionResult(False, "checkin", opened.detail, {"confirmed": False})
        st = self.checkin_status()
        if not st.get("ok"):
            return ActionResult(False, "checkin", f"read heatmap failed: {st.get('detail')}",
                                {"confirmed": False})
        if not st.get("switched", True):
            # 日历还没切到今天：此刻读到的绿色格子是昨天的，绝不能据此落库
            # （2026-11-13 00:00:51 就是这样误判，导致 11-13 真漏签）
            return ActionResult(
                False, "checkin",
                f"calendar still on {st.get('day_label') or 'previous day'}; "
                f"wait for date rollover before judging",
                {"confirmed": False, "switched": False,
                 "day_label": st.get("day_label")})
        streak = st.get("streak")
        makeup = st.get("makeup") or []
        data = {"confirmed": bool(st.get("checked")), "checked": st.get("checked"),
                "streak": streak, "makeup": makeup, "bg": st.get("bg"),
                "semantics": "activity-day"}
        if st.get("checked"):
            extra = f", streak={streak}d" if streak else ""
            if makeup:
                extra += f", missed={','.join(makeup)}"
            return ActionResult(True, "checkin", f"today recorded in heatmap{extra}", data)
        return ActionResult(
            False, "checkin",
            "today NOT recorded in activity heatmap (complete one conversation to check in)",
            data)
