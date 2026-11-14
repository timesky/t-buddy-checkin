"""左下角「Buddy加油站」浮层：每日签到的**唯一真实入口**。

2026-11-14 实测（5.11.3）：签到不在成长中心，而在主窗口左下角、用户名上方的
运营位浮层里。之前把签到判定改成「读成长中心的活跃地图」是错的 —— 活跃地图
只记录「有没有用客户端」，和「签到领 100 积分」是两套东西，于是 CDP 重构后
签到整条线其实一次都没做，却一直报 confirmed=True（假绿）。

实测 DOM（主 renderer，非 iframe）：

    div.daily-checkin.daily-checkin--bubble            <- 浮层容器
      div.wb-slot--avatar-top-combo  -> #shadowRoot     <- 远端 slot html 注入
        #h   标题          「Buddy加油站」
        #d   副标题        「每日可领 100 积分」
        #tt  轻提示        「领取成功」
        button.dot × N     轮播点，aria-label 标明每个卡片
        button#go          当前卡片的按钮
                           data-track-props={"index":0,"type":"checkin"}

**权威判据**（2026-11-14 二次更正）：不看卡片在不在、也不看 toast，而是读
React fiber 上 `AvatarTopComboSlot` 的 `checkinData` —— 那是宿主从
`/v2/billing/meter/checkin-activity-status` 拿到的服务端状态；配合
`onRefreshCheckin()` 可以现场重新拉一次。字段（服务端原始名）：

    today_checked_in : 今天是否已签
    today_credit     : 今天已领积分
    streak_days      : 连续签到天数
    checkin_dates    : 已签日期列表（含今天即已签）

用「卡片消失」当判据是不够的：卡片消失既可能是签到了，也可能是运营位换了
配置或被用户手动关掉；反过来卡片还在也不代表没签（隔天乐观展示）。
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from .base import ActionResult

ROOT = Path(__file__).resolve().parents[3]

# 浮层里签到卡片的标题（轮播点的 aria-label 也是它）
CHECKIN_LABEL = "Buddy加油站"
# 领取成功后的确定性文案（toast / 按钮），仅用于诊断展示
CLAIMED_MARKERS = ("领取成功", "已领取", "今日已领", "明天再来")


def _norm(t: str) -> str:
    return " ".join((t or "").split())


class FuelCard:
    """操作主窗口左下角浮层的签到卡片（CDP，不抢焦点）。"""

    def __init__(self, cfg: dict) -> None:
        g = cfg.get("growth", {}) or {}
        self.cfg = cfg
        self.port = int(g.get("cdp_port", 9222))
        self.node_bin = self._resolve_node()
        self.cdp_js = ROOT / "tools" / "cdp_client.mjs"
        self.block: tuple[str, str] | None = None

    # ---- 基础设施 -----------------------------------------------------------
    @staticmethod
    def _resolve_node() -> str | None:
        import os
        import shutil

        cand = shutil.which("node")
        if cand:
            return cand
        for p in (
            Path(os.environ.get("NODE_BIN", "")) if os.environ.get("NODE_BIN") else None,
            ROOT / "tools" / "node_modules" / ".bin" / "node",
            Path.home() / ".workbuddy/binaries/node/versions",
        ):
            if p and p.exists():
                if p.is_dir():
                    for sub in sorted(p.iterdir(), reverse=True):
                        exe = sub / "bin" / "node"
                        if exe.exists():
                            return str(exe)
                    continue
                return str(p)
        return None

    def _http(self, path: str) -> Any:
        import urllib.request

        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def page_ws(self) -> str | None:
        """主 renderer 的 ws（浮层在它里面，不是 iframe target）。"""
        try:
            data = self._http("/json/list")
        except Exception as exc:  # noqa: BLE001
            self.block = ("cdp_unavailable", f"CDP {self.port} unreachable: {exc}")
            return None
        for t in data if isinstance(data, list) else []:
            if t.get("type") != "page":
                continue
            url = (t.get("url") or "").lower()
            if "app.asar" in url or "workbuddy" in url:
                return t.get("webSocketDebuggerUrl")
        return None

    def _eval(self, js: str, timeout: int = 20) -> tuple[bool, Any]:
        ws = self.page_ws()
        if not ws:
            return False, (self.block or ("cdp_unavailable", "main window not found"))[1]
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

    # ---- 权威状态（React fiber → 服务端 checkinData） -----------------------
    STATUS_JS = r"""
(async () => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const pick = (o, ...keys) => {
    for (const k of keys) if (o && o[k] !== undefined && o[k] !== null) return o[k];
    return undefined;
  };
  let host = null;
  for (const h of document.querySelectorAll('*')) {
    if (h.shadowRoot && h.shadowRoot.getElementById('go')) { host = h; break; }
  }
  let props = null;
  if (host) {
    const key = Object.keys(host).find(k => k.startsWith('__reactFiber$') || k.startsWith('__reactProps$'));
    let f = key ? host[key] : null, depth = 0;
    while (f && depth < 30) {
      const p = f.memoizedProps || f.pendingProps || {};
      if (p && p.checkinData && (p.onRefreshCheckin || p.onCheckinClaim)) { props = p; break; }
      f = f.return; depth++;
    }
  }
  let data = props ? props.checkinData : null;
  let refresh_error = null;
  if (DATA_REFRESH && props && typeof props.onRefreshCheckin === 'function') {
    try {
      const fresh = await props.onRefreshCheckin();
      if (fresh) data = fresh;
    } catch (e) { refresh_error = String(e); }
  }
  if (!data) {
    return JSON.stringify({ok: false, reason: 'checkinData not found (slot not mounted)',
      host: !!host, refresh_error});
  }
  const dates = pick(data, 'checkin_dates', 'checkinDates') || [];
  return JSON.stringify({
    ok: true,
    active: !!pick(data, 'active'),
    today_checked_in: !!pick(data, 'today_checked_in', 'todayCheckedIn'),
    today_credit: pick(data, 'today_credit', 'todayCredit') ?? 0,
    daily_credit: pick(data, 'daily_credit', 'dailyCredit') ?? 0,
    streak_days: pick(data, 'streak_days', 'streakDays') ?? 0,
    total_credits: pick(data, 'total_credits', 'totalCredits') ?? 0,
    checkin_dates: dates.slice(0, 60),
    theme_name: pick(data, 'theme_name', 'themeName') || '',
    claim_button_text: pick(data, 'claim_button_text', 'claimButtonText') || '',
    refresh_error,
    refreshed: DATA_REFRESH,
    has_props: !!props
  });
})()
"""

    def status(self, refresh: bool = True) -> dict:
        """读签到状态；refresh=True 时现场向服务端重新拉一次。

        返回 {ok, today_checked_in, today_credit, streak_days, checkin_dates, ...}
        ok=False 表示浮层未挂载/读不到，**不能**据此判断「已签」。
        """
        js = self.STATUS_JS.replace(
            "DATA_REFRESH", "true" if refresh else "false")
        ok, val = self._eval(js, timeout=25)
        if not ok:
            return {"ok": False, "reason": str(val), "today_checked_in": None}
        return val if isinstance(val, dict) else {"ok": False, "reason": "bad payload"}

    # ---- 读取（诊断用：卡片/轮播/toast） ------------------------------------
    PROBE_JS = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const out = [];
  for (const host of document.querySelectorAll('*')) {
    const sr = host.shadowRoot;
    if (!sr) continue;
    const go = sr.getElementById('go') || sr.querySelector(':scope button.go');
    if (!go) continue;
    const dots = [...sr.querySelectorAll('.dot')].map(d => ({
      on: d.getAttribute('data-on') === '1',
      label: d.getAttribute('aria-label') || ''
    }));
    const el = id => sr.getElementById(id);
    let props = {};
    try { props = JSON.parse(go.getAttribute('data-track-props') || '{}'); } catch (e) {}
    out.push({
      host_cls: String(host.className || '').slice(0, 80),
      title: norm((el('h') || {}).textContent || ''),
      sub: norm((el('d') || {}).textContent || ''),
      toast: norm((el('tt') || {}).textContent || ''),
      btn: norm(go.textContent || ''),
      btn_disabled: !!go.disabled,
      props,
      dots,
      visible: host.getBoundingClientRect().width > 0
    });
  }
  return JSON.stringify({cards: out});
})()
"""

    def probe(self, switch_to_checkin: bool = False) -> dict:
        """读浮层外观状态（轮播/当前卡/按钮），仅诊断用，不作签到判据。"""
        ok, val = self._eval(self.PROBE_JS)
        if not ok:
            return {"ok": False, "present": False, "claimed": None,
                    "detail": str(val), "cards": []}
        cards = (val or {}).get("cards") or []
        if not cards:
            return {"ok": True, "present": False, "claimed": None,
                    "detail": "fuel card not rendered (closed, logged out or no slot config)",
                    "cards": []}

        for card in cards:
            labels = [d.get("label") for d in (card.get("dots") or [])]
            card["has_checkin_item"] = CHECKIN_LABEL in labels
            card["active_is_checkin"] = (card.get("props") or {}).get("type") == "checkin"
            if card["has_checkin_item"] and not card["active_is_checkin"]:
                card["checkin_index"] = labels.index(CHECKIN_LABEL)
        card = next((c for c in cards if c.get("has_checkin_item")
                     or c.get("active_is_checkin")), cards[0])
        if switch_to_checkin and card.get("has_checkin_item") and not card.get("active_is_checkin"):
            idx = card.get("checkin_index", 0)
            ok2, _ = self._eval(self._switch_js(idx))
            if ok2:
                time.sleep(0.6)
                again = self.probe(switch_to_checkin=False)
                if again.get("cards"):
                    card = again["card"]

        if card.get("active_is_checkin"):
            claimed = any(m in (card.get("toast") or "") or m in (card.get("btn") or "")
                          for m in CLAIMED_MARKERS)
            present = not claimed and not card.get("btn_disabled")
        else:
            present = bool(card.get("has_checkin_item"))
            claimed = None if present else True
        return {"ok": True, "present": present, "claimed": claimed,
                "card": card, "cards": cards, "detail": "ok"}

    @staticmethod
    def _switch_js(idx: int) -> str:
        return (
            "(() => {"
            "  const hosts = [...document.querySelectorAll('*')].filter(h => h.shadowRoot);"
            "  for (const h of hosts) {"
            "    const dots = [...h.shadowRoot.querySelectorAll('.dot')];"
            f"    if (dots.length > {idx}) {{ dots[{idx}].click(); return JSON.stringify({{ok:true}}); }}"
            "  }"
            "  return JSON.stringify({ok:false, reason:'dot not found'});"
            "})()"
        )

    # ---- 浮层是否可点 -------------------------------------------------------
    def ensure_visible(self) -> tuple[bool, str]:
        """确保签到浮层在 DOM 里（被关掉时从用户菜单重新唤起）。"""
        ok, val = self._eval(
            "(() => { for (const h of document.querySelectorAll('*')) {"
            "  if (h.shadowRoot && h.shadowRoot.getElementById('go')) "
            "    return JSON.stringify({ok:true}); } "
            "  return JSON.stringify({ok:false}); })()")
        if ok and isinstance(val, dict) and val.get("ok"):
            return True, "visible"

        # 浮层被关掉：从侧栏用户菜单的「Buddy加油站」重新打开
        reopen = (
            "(() => {"
            "  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();"
            "  const item = [...document.querySelectorAll('[data-track-id]')]"
            "    .find(e => /fuel_station|menu_signin|fuel/.test(e.getAttribute('data-track-id') || ''));"
            "  if (item) { item.click(); return JSON.stringify({ok:true, how:'menu_item'}); }"
            "  const trigger = document.querySelector('[data-track-id=\"user_avatar_menu\"]')"
            "    || document.querySelector('.user-menu-trigger');"
            "  if (!trigger) return JSON.stringify({ok:false, reason:'no menu trigger'});"
            "  trigger.click();"
            "  return JSON.stringify({ok:true, how:'open_menu'});"
            "})()")
        ok2, val2 = self._eval(reopen)
        if not ok2 or not (isinstance(val2, dict) and val2.get("ok")):
            return False, f"cannot reopen fuel card: {val2}"
        if (val2 or {}).get("how") == "open_menu":
            time.sleep(0.9)
            ok3, val3 = self._eval(
                "(() => {"
                "  const item = document.querySelector('[data-track-id=\"avatar_menu_fuel_station\"]');"
                "  if (!item) return JSON.stringify({ok:false, reason:'fuel menu item missing'});"
                "  item.click(); return JSON.stringify({ok:true});"
                "})()")
            if not ok3 or not (isinstance(val3, dict) and val3.get("ok")):
                return False, f"cannot reopen fuel card: {val3}"

        for _ in range(10):
            time.sleep(0.4)
            ok4, val4 = self._eval(
                "(() => { for (const h of document.querySelectorAll('*')) {"
                "  if (h.shadowRoot && h.shadowRoot.getElementById('go')) "
                "    return JSON.stringify({ok:true}); } "
                "  return JSON.stringify({ok:false}); })()")
            if isinstance(val4, dict) and val4.get("ok"):
                return True, "reopened"
        return False, "fuel card did not reappear after reopen"

    # ---- 动作 ---------------------------------------------------------------
    def claim(self, wait_sec: float = 25.0) -> ActionResult:
        """点浮层「立即领取」完成今日签到，用服务端状态确认。"""
        st = self.status(refresh=True)
        if not st.get("ok"):
            return ActionResult(
                False, "checkin",
                f"cannot read checkin status: {st.get('reason') or st.get('refresh_error')}",
                {"confirmed": False, "source": "fuel-card"})
        if st.get("today_checked_in"):
            return ActionResult(
                True, "checkin",
                f"already checked in today (credit={st.get('today_credit')}, "
                f"streak={st.get('streak_days')}d)",
                {"confirmed": True, "already": True, "source": "fuel-card",
                 "today_credit": st.get("today_credit"), "streak_days": st.get("streak_days")})

        visible, why = self.ensure_visible()
        if not visible:
            return ActionResult(False, "checkin", why,
                                {"confirmed": False, "source": "fuel-card"})

        ok, val = self._eval(self._click_js())
        info = val if isinstance(val, dict) else {}
        if not ok or not info.get("ok"):
            # 卡片不在轮播里（例如已下线/被关闭）时退化为直接触发宿主事件
            ok2, val2 = self._eval(
                "(() => { const B = window.__HOST_BRIDGE__; if (!B || !B.emit) "
                "return JSON.stringify({ok:false}); B.emit('checkin_clicked'); "
                "return JSON.stringify({ok:true, how:'emit'}); })()")
            if not ok2 or not (isinstance(val2, dict) and val2.get("ok")):
                return ActionResult(
                    False, "checkin",
                    f"claim button unavailable: {info.get('reason') or val}",
                    {"confirmed": False, "source": "fuel-card"})

        deadline = time.time() + wait_sec
        last: dict = {}
        while time.time() < deadline:
            time.sleep(1.5)
            last = self.status(refresh=True)
            if last.get("ok") and last.get("today_checked_in"):
                return ActionResult(
                    True, "checkin",
                    f"claimed: credit={last.get('today_credit')}, "
                    f"streak={last.get('streak_days')}d",
                    {"confirmed": True, "source": "fuel-card",
                     "today_credit": last.get("today_credit"),
                     "streak_days": last.get("streak_days")})
        return ActionResult(
            False, "checkin",
            f"clicked but server still says not checked in after {wait_sec:.0f}s "
            f"(today_credit={last.get('today_credit')}, "
            f"refresh_error={last.get('refresh_error')})",
            {"confirmed": False, "source": "fuel-card"})

    @staticmethod
    def _click_js() -> str:
        return (
            "(() => {"
            "  for (const h of document.querySelectorAll('*')) {"
            "    const sr = h.shadowRoot; if (!sr) continue;"
            "    const go = sr.getElementById('go'); if (!go) continue;"
            "    let p = {}; try { p = JSON.parse(go.getAttribute('data-track-props') || '{}'); } catch (e) {}"
            "    if (p.type !== 'checkin') continue;"
            "    if (go.disabled) return JSON.stringify({ok:false, reason:'disabled'});"
            "    go.click();"
            "    return JSON.stringify({ok:true, btn:(go.textContent||'').trim()});"
            "  }"
            "  return JSON.stringify({ok:false, reason:'checkin button not found'});"
            "})()"
        )
