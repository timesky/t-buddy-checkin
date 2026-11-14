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

点击 #go 会经 bridge 事件 checkin_clicked 触发宿主 claimDailyCheckin()
（POST /v2/billing/meter/daily-checkin），成功后该卡片从轮播里消失、轮播自动
切到下一张，因此「加油站卡片是否还在」就是最可靠的签到状态。

补签不在浮层：漏签只能去成长中心日历点漏签格，消耗补登卡（本模块不管）。
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
# 领取成功后的确定性文案（toast / 按钮）
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

    # ---- 读取 ---------------------------------------------------------------
    # 只在 shadow DOM 里读：浮层由远端 slot html 渲染，light DOM 永远是空的，
    # 之前查 textContent 为空就以为「浮层没出现」。
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
        """读浮层状态。

        返回 {ok, present, claimed, card:{...}, cards:[...], detail}
        present = 轮播里还有「Buddy加油站」这张卡（=今天还能领）
        """
        ok, val = self._eval(self.PROBE_JS)
        if not ok:
            return {"ok": False, "present": False, "claimed": None,
                    "detail": str(val), "cards": []}
        cards = (val or {}).get("cards") or []
        if not cards:
            # 浮层不在（被关掉 / 未登录 / 运营位未下发）：状态未知，绝不判「已签」
            return {"ok": True, "present": False, "claimed": None,
                    "detail": "fuel card not rendered (closed, logged out or no slot config)",
                    "cards": []}

        for card in cards:
            labels = [d.get("label") for d in (card.get("dots") or [])]
            has_checkin_item = CHECKIN_LABEL in labels
            active_is_checkin = (card.get("props") or {}).get("type") == "checkin"
            card["has_checkin_item"] = has_checkin_item
            card["active_is_checkin"] = active_is_checkin
            if has_checkin_item and not active_is_checkin:
                card["checkin_index"] = labels.index(CHECKIN_LABEL)
        card = next((c for c in cards if c.get("has_checkin_item")
                     or c.get("active_is_checkin")), cards[0])
        if switch_to_checkin and card.get("has_checkin_item") and not card.get("active_is_checkin"):
            idx = card.get("checkin_index", 0)
            ok2, _ = self._eval(self._switch_js(idx))
            if ok2:
                time.sleep(0.8)
                again = self.probe(switch_to_checkin=False)
                if again.get("cards"):
                    card = again["card"]

        if card.get("active_is_checkin"):
            claimed = any(m in (card.get("toast") or "") or m in (card.get("btn") or "")
                          for m in CLAIMED_MARKERS)
            present = not claimed and not card.get("btn_disabled")
        else:
            # 当前卡片不是签到卡：签到项还在轮播里 -> 未领；不在了 -> 今天已领/已下线
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

    # ---- 动作 ---------------------------------------------------------------
    def claim(self, wait_sec: float = 6.0) -> ActionResult:
        """点浮层「立即领取」完成今日签到，并确认真的领到了。"""
        st = self.probe(switch_to_checkin=True)
        if not st.get("ok"):
            return ActionResult(False, "checkin", st.get("detail") or "probe failed",
                                {"confirmed": False, "source": "fuel-card"})
        card = st.get("card") or {}
        if st.get("present") is False and st.get("claimed") is True:
            return ActionResult(True, "checkin", "already claimed today (fuel card gone)",
                                {"confirmed": True, "already": True, "source": "fuel-card"})
        if not st.get("present"):
            return ActionResult(
                False, "checkin",
                f"fuel card not claimable: {st.get('detail')} "
                f"(toggle={[(d.get('label'), d.get('on')) for d in card.get('dots') or []]})",
                {"confirmed": False, "source": "fuel-card"})

        ok, val = self._eval(self._click_js())
        if not ok:
            return ActionResult(False, "checkin", f"click failed: {val}",
                                {"confirmed": False, "source": "fuel-card"})
        info = val if isinstance(val, dict) else {}
        if not info.get("ok"):
            return ActionResult(False, "checkin",
                                f"claim button unavailable: {info.get('reason')}",
                                {"confirmed": False, "source": "fuel-card"})

        deadline = time.time() + wait_sec
        last = {}
        while time.time() < deadline:
            time.sleep(0.6)
            after = self.probe()
            last = after
            toast = ((after.get("card") or {}).get("toast") or "")
            if any(m in toast for m in CLAIMED_MARKERS):
                return ActionResult(True, "checkin", f"claimed: {toast}",
                                    {"confirmed": True, "toast": toast,
                                     "source": "fuel-card"})
            if after.get("present") is False:
                return ActionResult(True, "checkin", "claimed: checkin card removed",
                                    {"confirmed": True, "source": "fuel-card"})
        return ActionResult(
            False, "checkin",
            f"clicked but no confirmation in {wait_sec}s "
            f"(toast={(last.get('card') or {}).get('toast')!r}, "
            f"btn={(last.get('card') or {}).get('btn')!r})",
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
