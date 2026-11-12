"""成长中心适配器（CDP 版）：直连 WorkBuddy 客户端内嵌 iframe，DOM 级操作。

取代旧的 CuaDriver/AX 通道（GrowthCenter）。原理（2026-11-12 实测）：

  - WorkBuddy 客户端是 Electron。设置环境变量 WORKBUDDY_REMOTE_DEBUGGING_PORT
    后主进程会 appendSwitch('remote-debugging-port')，暴露 CDP。
  - 成长中心是 https://www.workbuddy.cn/profile/growth-center，以 **OOPIF**
    （type=iframe）出现在 /json/list 里，可直连 WebSocket 执行 Runtime.evaluate。
  - 打开入口（侧栏「成长计划」→「立即前往」）不在主 renderer 的 DOM 里，
    只能靠 agent-browser 的跨 webContents 聚合 snapshot/click 触发。
  - 页面打开后，所有读取与点击都在 iframe 内用 JS 完成，比 AX 快且稳。

签到语义变更（2026-11-12 实测 5.11.2）：客户端已无「Buddy加油站 / 立即签到」
按钮，签到 = 每天在活跃地图留下一条记录（完成一次对话即可）。因此签到不再是
「点按钮」，而是读日历状态：
    .gs-cal-cell.is-today  今日格子
    绿色 rgb(111, 229, 198) 或 is-checked = 今天已打卡
    .gs-cal-cell.is-broken 漏签（可补，补签卡有限，永不自动消耗）
"""
from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .base import ActionResult

ROOT = Path(__file__).resolve().parents[3]

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
        self.block: tuple[str, str] | None = None
        self.last_close_ok: bool = False
        self._owner_ok: bool = False
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

    def ensure_page(self) -> ActionResult:
        """确保成长中心 iframe 存在且已渲染；不存在就从客户端入口打开。"""
        if self._iframe_ws():
            ready = self._wait_page_ready()
            return ActionResult(True, "growth_open", "page already open",
                                {"opened": False, "ready": ready})
        if not self.available():
            code, msg = self.block or ("cdp_unavailable", "")
            return ActionResult(False, "growth_open", msg, {"blocked": code})

        if not self._ensure_connected():
            return ActionResult(False, "growth_open", "agent-browser connect failed")

        labels = ["成长计划"] + list((self.cfg.get("growth", {}) or {}).get("open_labels") or [])
        opened = False
        for label in labels:
            ok, detail = self._click_label(label)
            if not ok:
                continue
            time.sleep(1.0)
            # 点入口后会出现「立即前往」
            ok2, _ = self._click_label("立即前往", attempts=2)
            opened = True
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
            _ = detail
        return ActionResult(
            False, "growth_open",
            "opened entry but growth-center iframe never appeared" if opened
            else "no growth entry found in client",
        )

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
  let checked = false, bg = '';
  if (today) {
    bg = getComputedStyle(today).backgroundColor || '';
    checked = today.classList.contains('is-checked') || /111,\s*229,\s*198/.test(bg);
  }
  const makeup = cells
    .filter(c => c.classList.contains('is-broken'))
    .map(c => norm(c.textContent));
  const m = norm(document.body.innerText).match(/当前连续登录\s*(\d+)\s*天/);
  return JSON.stringify({
    has_today: !!today, checked, bg, makeup,
    streak: m ? parseInt(m[1], 10) : null,
    days: cells.length
  });
})()
"""

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
        ok, val = self._eval(self.CHECKIN_JS)
        if not ok:
            return {"ok": False, "detail": str(val)}
        info = val if isinstance(val, dict) else {}
        return {
            "ok": True,
            "checked": bool(info.get("checked")),
            "has_today": bool(info.get("has_today")),
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
                time.sleep(1.0)
                self._close_dialog()
                if not ok2 and any(m in str(click_label) + target for m in EXCHANGE_NO_MORE_MARKERS):
                    results.append(ActionResult(
                        True, "exchange", f"exchange concluded: {target}",
                        {"kind": kind, "key": key, "skipped": True, "conclusive": True}))
                    continue
                results.append(ActionResult(
                    ok2, "exchange", f"milestone claimed: {target}",
                    {"kind": kind, "key": key, "label": target}))
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
            f"dispatched; real duration {info['remaining_sec']}s",
            {"remaining_sec": info["remaining_sec"], "due_at": due,
             "scene": self.scene or "default"})

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

        if state in ("claimable", "gift_dialog") or force:
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

        if state == "idle":
            dispatched = self.dispatch()
            data = {"state": state, "due_at": dispatched.data.get("due_at"),
                    "dispatch": dispatched.as_dict()}
            if dispatched.data.get("dispatch_blocked"):
                data["dispatch_blocked"] = dispatched.data["dispatch_blocked"]
            return ActionResult(dispatched.ok, "travel", dispatched.detail, data)

        return ActionResult(False, "travel", f"unknown growth-center state: {state}", info)

    # ---- 签到（状态校验） ---------------------------------------------------
    @_auto_close
    def checkin(self) -> ActionResult:
        """签到 = 活跃地图今日是否已有记录；不再点击任何按钮。"""
        opened = self.ensure_page()
        if not opened.ok:
            return ActionResult(False, "checkin", opened.detail, {"confirmed": False})
        st = self.checkin_status()
        if not st.get("ok"):
            return ActionResult(False, "checkin", f"read heatmap failed: {st.get('detail')}",
                                {"confirmed": False})
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
