"""成长中心适配器：用 CuaDriver 的 AX 通道操作已登录的成长中心页面。

**不限定浏览器**：从 WorkBuddy 点开进入哪个浏览器（Chrome / Edge / Safari 等），
就在哪个浏览器操作；默认 browser_app="auto"，按页面内容（而非固定 app）探测窗口。
如页面未登录，只提示用户先登录，不做任何自动登录尝试。

真实页面 https://www.workbuddy.cn/profile/growth-center 的 AX 状态（2026-11-09 实测）：

  idle      : AXButton "派猫猫旅行"
  traveling : AXButton "旅行倒计时 00:59:56" + "Buddy 正在咖啡馆采风中..."
  claimable : AXButton "领取礼物" -> 弹窗 -> AXButton "领取 7 积分" -> "已领取"

派遣前只提示「预计出行时间为1～4小时」，**实际时长每次不同**，必须派遣后
从倒计时文本读取真实剩余秒数，再换算成北京时间的绝对到期时间戳。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .base import ActionResult

CUA_DRIVER_BIN = "/Applications/CuaDriver.app/Contents/MacOS/cua-driver"

# "旅行倒计时 00:59:56" / "00:59:57"
_COUNTDOWN = re.compile(r"(\d{1,2}):([0-5]\d):([0-5]\d)")

# 登录页特征：命中即判定为「未登录」，提示用户登录，绝不自动登录。
# 注意只保留强特征——「请登录 / 登录后」等泛化文案在已登录页面也会出现
# （实测 2026-11-09：侧栏「用户配置 1 个人资料，请登录」曾造成误判），不列入。
LOGIN_MARKERS = (
    "微信扫码登录", "wechat-qrcode", "扫码登录", "立即登录",
    "账号密码登录", "短信登录", "login",
)
# 成长中心页面的强内容特征（真实按钮/文案，登录页不会有）：
# 判定时优先于登录标记——已登录页面上夹杂的登录类文案不能否决成长内容。
GROWTH_CONTENT_MARKERS = (
    "派猫猫旅行", "领取礼物", "旅行倒计时", "兑换奖励", "Buddy 正在",
    "开启盲盒", "确定派出",
)
# 弱特征（窗口标题等）：仅用于候选窗口排序，不参与内容判定
GROWTH_MARKERS = GROWTH_CONTENT_MARKERS + ("个人中心", "连签", "我的积分")

# 动作完成后需要清掉的页面浮层。
DIALOG_HINTS = (
    "已领取", "领取成功", "恭喜", "恭喜获得", "已获得", "获取成功", "领取奖励",
    "选择奖励", "我知道了", "关闭", "还差", "今日已领", "兑换成功",
)
# 精确匹配才点，排除「关闭标签页 / 关闭窗口」等浏览器自身控件。
# 2026-11-10 实测：领奖浮层的关闭按钮未必叫「关闭」，需覆盖常见变体；
# 匹配不到时用 Escape 兜底，仍关不掉再判定弹窗遮挡。
DIALOG_CLOSE_LABELS = (
    "关闭", "×", "✕", "X", "我知道了", "知道了", "好的", "收到",
    "完成", "确定", "确认", "稍后再说", "收起", "朕知道了",
)
# 派遣被禁用时的次数用尽文案
DISPATCH_EXHAUSTED_MARKERS = (
    "今日已派", "次数已用完", "次数用尽", "今日次数", "已达上限",
    "明日再来", "明天再来", "派遣次数", "已派完", "无可派遣",
)


def parse_hms(text: str) -> int | None:
    """从任意文本里抽 HH:MM:SS 并返回总秒数。"""
    m = _COUNTDOWN.search(text or "")
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


class GrowthCenter:
    def __init__(self, cfg: dict, runner: Callable[..., Any] | None = None,
                 state: Any | None = None) -> None:
        growth = cfg.get("growth", {})
        self.state = state  # 可选：把自动打开冷却写到 kv，跨进程/重启生效
        self.bin = growth.get("cua_driver_bin") or CUA_DRIVER_BIN
        # 默认 auto：探测任意浏览器里已打开的成长中心窗口，不限定 Edge
        self.browser_app = growth.get("browser_app", "auto") or "auto"
        self.title_hint = growth.get("title_hint", "个人中心")
        self.claim_offset_sec = float(growth.get("claim_offset_sec", 3))
        self.scene = growth.get("scene", "")  # 留空 = 用页面默认场景
        # 浏览器里没有成长中心页面时，自动从 WorkBuddy 客户端入口触发打开
        self.desktop_app = (cfg.get("desktop", {}) or {}).get("app", "WorkBuddy")
        # 客户端窗口里可能显示包含「派猫猫旅行」等字样的文档/对话，必须排除，
        # 否则会被误判成成长中心页面（2026-11-10 实测踩坑）
        self.exclude_apps = growth.get("exclude_apps") or [self.desktop_app]
        self.auto_open_page = bool(growth.get("auto_open_page", True))
        self.auto_open_labels = growth.get("auto_open_labels") or [
            "做任务赢积分", "成长计划", "任务中心", "Buddy加油站",
        ]
        self.auto_open_wait = float(growth.get("auto_open_wait_sec", 6))
        self.auto_open_cooldown = float(growth.get("auto_open_cooldown_sec", 300))
        self.auto_open_url = growth.get("auto_open_url") or \
            "https://www.workbuddy.cn/profile/growth-center"
        self.auto_open_url_fallback = bool(growth.get("auto_open_url_fallback", True))
        # 冷却从 kv 读，进程重启后仍生效（避免每次重启都新开页签）
        persisted = state.kv_get_float("last_auto_open_at") if state is not None else None
        self._last_auto_open = float(persisted or 0.0)
        self.runner = runner or subprocess.run
        self.commands: list[list[str]] = []
        # 上一次 _window 探窗结论：("login_required"|"not_found", detail)
        self._block: tuple[str, str] | None = None

    # ---- CuaDriver 调用 -----------------------------------------------------
    def _call(self, tool: str, payload: dict) -> dict:
        cmd = [self.bin, "call", tool, json.dumps(payload, ensure_ascii=False)]
        self.commands.append(cmd)
        try:
            proc = self.runner(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"error": str(exc)}
        raw = getattr(proc, "stdout", "") or ""
        i = raw.find("{")
        if i < 0:
            return {"error": (raw or getattr(proc, "stderr", "") or "empty output")[:200]}
        try:
            return json.loads(raw[i:])
        except ValueError:
            return {"error": "invalid JSON from cua-driver"}

    @staticmethod
    def _classify(snap: dict) -> str:
        """判定快照属于 成长中心页面 / 登录页 / 其他。

        **只认 AXButton 的标签**作为成长中心的强内容证据：真实页面里
        「派猫猫旅行 / 领取礼物 / 旅行倒计时 / 兑换奖励」都是按钮；而客户端
        里打开的 README/对话中同样字样是 AXStaticText/AXTextArea，不能算。
        登录页则用全部文本判定（登录提示常是静态文本）。
        """
        if not isinstance(snap, dict):
            return "other"
        els = snap.get("elements", [])
        buttons = " ".join(
            str(e.get("label") or "") for e in els
            if e.get("role") in ("AXButton", "AXPopUpButton")
        )
        all_text = f"{snap.get('raw', '')} " + " ".join(
            str(e.get("label") or "") for e in els
        )
        if any(m in buttons for m in GROWTH_CONTENT_MARKERS):
            return "growth"
        if any(m in all_text for m in LOGIN_MARKERS):
            return "login"
        return "other"

    def _window(self) -> tuple[int, int] | None:
        """找成长中心窗口；找不到且开启 auto_open_page 时自动触发打开页面。

        不限定浏览器：browser_app="auto" 时按页面内容判定；命中登录页则
        记为 login_required 并提示用户登录（此时**不**自动点任何东西）。
        """
        win = self._locate()
        if win:
            return win
        if self.auto_open_page and self._block and self._block[0] == "not_found":
            # 先判断：如果已经存在 WorkBuddy/个人中心 页签，只是暂时读不到内容
            # （加载中、被切到后台），绝不再新开页签——这是 2026-11-10 用户反馈的
            # 「每次都新开 WorkBuddy 页签」根因。
            if self._has_existing_page_tab():
                self._block = (
                    "not_found",
                    "已存在个人中心/成长中心页签，但当前读不到内容"
                    "（可能加载中或切到后台）；不重复新开页签，稍后重试",
                )
                return None
            opened_entry = False
            opened_url = False
            now = time.time()
            if now - self._last_auto_open >= self.auto_open_cooldown:
                self._last_auto_open = now
                if self.state is not None:
                    self.state.kv_set("last_auto_open_at", now)
                # 一、优先点用户指定的客户端入口（一轮、只点一个，避免连锁点错）
                if self._try_auto_open():
                    opened_entry = True
                    win = self._locate()
                    if win:
                        return win
                # 二、兜底：后台打开目标 URL（-g 不激活浏览器、不抢焦点）
                if self.auto_open_url_fallback and self._open_url_fallback():
                    opened_url = True
                    win = self._locate()
                    if win:
                        return win
            did = "、".join(
                p for p, ok in (("点击客户端入口", opened_entry),
                                ("后台打开成长中心 URL", opened_url)) if ok
            ) or "未执行自动打开"
            self._block = (
                "not_found",
                f"{did}，但仍未检测到页面：请确认页面已加载且已登录"
                "（www.workbuddy.cn/profile/growth-center）",
            )
        return None

    def _has_existing_page_tab(self) -> bool:
        """是否已有目标页签（按窗口标题判断），避免重复新开。

        只把「明确指向 WorkBuddy 个人中心/成长中心」的标题算作已有页签；
        CodeBuddy 等其它产品的「个人中心」不算，以免把错页当成目标页。
        """
        data = self._call("list_windows", {})
        for w in data.get("windows", []):
            if (w.get("bounds") or {}).get("width", 0) <= 400:
                continue
            title = str(w.get("title") or "")
            low = title.lower()
            if "growth-center" in low or "growth_center" in low:
                return True
            if "workbuddy" in low and any(
                    k in title for k in ("个人中心", "成长中心", "成长计划")):
                return True
        return False

    def _locate(self) -> tuple[int, int] | None:
        """只读探测：扫描可见窗口，按 AX 内容找成长中心页面。"""
        self._block = None
        data = self._call("list_windows", {})
        if "error" in data:
            # CuaDriver daemon 挂掉时不能当成「没窗口」，否则会盲目 open -g 反复开页签
            # （2026-11-10 14:42/15:42 实测）。daemon 不可用时一律不自动打开。
            self._block = (
                "driver_unavailable",
                f"CuaDriver daemon 不可用（{str(data['error'])[:120]}）；"
                "本轮不自动打开页签。可执行 cua-driver permissions grant 重新拉起 daemon",
            )
            return None
        wins = data.get("windows", [])
        if self.browser_app != "auto":
            wins = [w for w in wins if w.get("app_name") == self.browser_app]
        wins = [w for w in wins
                if (w.get("bounds") or {}).get("width", 0) > 400
                and w.get("app_name") not in self.exclude_apps]
        if not wins:
            self._block = ("not_found",
                           "未找到任何浏览器窗口：请从 WorkBuddy 点开「个人中心」"
                           "(www.workbuddy.cn/profile/growth-center) 再运行")
            return None
        ranked = sorted(
            wins,
            key=lambda w: (
                (self.title_hint in str(w.get("title") or "")
                 or "workbuddy" in str(w.get("title") or "").lower()),
                w["bounds"]["width"] * w["bounds"]["height"],
            ),
            reverse=True,
        )
        for w in ranked[:4]:
            pid, wid = int(w["pid"]), int(w["window_id"])
            snap = self._call("get_window_state", {
                "pid": pid, "window_id": wid,
                "include_screenshot": False, "max_elements": 1500,
            })
            if "error" in snap:
                continue
            cls = self._classify(snap)
            if cls == "growth":
                return pid, wid
            if cls == "login":
                self._block = ("login_required",
                               "成长中心页面未登录：请在浏览器登录 WorkBuddy 后，"
                               "重新打开个人中心页面，再运行本命令")
                return None
        self._block = ("not_found",
                       "已找到浏览器窗口，但其中没有成长中心页面：请确认已打开 "
                       "www.workbuddy.cn/profile/growth-center 且已登录")
        return None

    # ---- 自动打开成长中心页面 ------------------------------------------------
    def _desktop_window(self) -> tuple[int, int] | None:
        data = self._call("list_windows", {})
        wins = [
            w for w in data.get("windows", [])
            if w.get("app_name") == self.desktop_app
            and w.get("is_on_screen")
            and (w.get("bounds") or {}).get("width", 0) > 400
        ]
        if not wins:
            return None
        w = max(wins, key=lambda x: x["bounds"]["width"] * x["bounds"]["height"])
        return int(w["pid"]), int(w["window_id"])

    def _click_raw(self, el: dict, pid: int, wid: int) -> str | None:
        """纯 AX 点击，成功返回 None，失败返回原因（不移动光标）。"""
        payload: dict[str, Any] = {"pid": pid, "window_id": wid}
        if el.get("element_token"):
            payload["element_token"] = el["element_token"]
        elif el.get("element_index") is not None:
            payload["element_index"] = el["element_index"]
        else:
            return "element has no token/index"
        out = self._call("click", payload)
        if "error" in out:
            return str(out["error"])
        if out.get("path") not in (None, "ax"):
            return f"non-AX click path: {out.get('path')}"
        return None

    # 允许尝试的角色：不同客户端版本里入口可能是按钮、链接或纯文本卡片
    _ENTRY_ROLES = ("AXButton", "AXMenuItem", "AXLink", "AXCell", "AXStaticText")

    def _try_auto_open(self) -> bool:
        """从 WorkBuddy 客户端入口触发打开成长中心页面（只点一个入口）。

        实测 2026-11-10：面板收起时是 `AXStaticText "做任务赢积分好礼"` +
        `AXButton "Buddy加油站"`；「成长计划」实测打开的是 CodeBuddy 积分页，
        已移出默认候选。这里**不做第二轮连锁点击**，避免点错入口覆盖正确页面。
        任何元素点击失败（如 AXStaticText 不支持 AXPress）都继续试下一个候选。
        返回是否真的点了一个入口。
        """
        win = self._desktop_window()
        if not win:
            return False
        pid, wid = win
        snap = self._call("get_window_state", {
            "pid": pid, "window_id": wid,
            "include_screenshot": False, "max_elements": 2000,
        })
        if "error" in snap:
            return False
        for label in self.auto_open_labels:
            for el in snap.get("elements", []):
                lab = str(el.get("label") or "").strip()
                if label not in lab or el.get("role") not in self._ENTRY_ROLES:
                    continue
                if self._click_raw(el, pid, wid) is not None:
                    continue  # 该元素不能 press，试下一个
                time.sleep(self.auto_open_wait)
                return True
        return False

    def _open_url_fallback(self) -> bool:
        """后台打开成长中心 URL；open -g 不激活浏览器、不抢焦点。"""
        try:
            proc = subprocess.run(
                ["open", "-g", self.auto_open_url],
                capture_output=True, text=True, timeout=10,
            )
            ok = proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
        if ok:
            time.sleep(self.auto_open_wait)
        return ok

    def _block_result(self, action: str) -> ActionResult:
        kind, detail = self._block or ("not_found", "growth-center window not found")
        return ActionResult(False, action, detail, {"block": kind})

    def snapshot(self, pid: int | None = None, wid: int | None = None) -> dict:
        if pid is None or wid is None:
            win = self._window()
            if not win:
                return {"error": (self._block or ("not_found", "growth-center window not found"))[1]}
            pid, wid = win
        return self._call("get_window_state", {
            "pid": pid, "window_id": wid,
            "include_screenshot": False, "max_elements": 3000,
        })

    def _click(self, snap: dict, predicate, pid: int, wid: int,
               _retries: int = 1) -> tuple[bool, str]:
        for el in snap.get("elements", []):
            if el.get("role") != "AXButton":
                continue
            label = str(el.get("label") or "")
            if predicate(label):
                payload: dict[str, Any] = {"pid": pid, "window_id": wid}
                if el.get("element_token"):
                    payload["element_token"] = el["element_token"]
                elif el.get("element_index") is not None:
                    payload["element_index"] = el["element_index"]
                else:
                    continue
                out = self._call("click", payload)
                if "error" in out:
                    # -25202 / stale：元素引用失效，重新快照定位再试一次
                    if _retries > 0:
                        return self._click(self.snapshot(pid, wid), predicate,
                                           pid, wid, _retries - 1)
                    return False, str(out["error"])
                if out.get("path") not in (None, "ax"):
                    # 非 AX 路径会移动光标，坚决拒绝
                    return False, f"refused non-AX click path: {out.get('path')}"
                return True, label
        return False, "button not found"

    # ---- 状态解析 -----------------------------------------------------------
    @staticmethod
    def read_state(snap: dict) -> dict[str, Any]:
        """从 AX 快照读真实旅行状态与剩余秒数。

        状态判定**只看 AXButton**：这些在真实页面里都是按钮；文档/对话中
        同样的字样是静态文本，绝不能用来驱动状态机。raw 仍保留全部文本，
        供「已领取」等_confirmation 文案检索。
        """
        els = snap.get("elements", [])
        labels: list[str] = []
        buttons: list[str] = []
        for el in els:
            lab = str(el.get("label") or "")
            if not lab or lab == "None":
                continue
            labels.append(lab)
            if el.get("role") in ("AXButton", "AXPopUpButton"):
                buttons.append(lab)
        joined = " | ".join(labels[:250])
        state = "unknown"
        remaining = None
        countdown = [b for b in buttons if "旅行倒计时" in b]
        if countdown:
            state = "traveling"
            for lab in countdown:
                remaining = parse_hms(lab)
                if remaining is not None:
                    break
            if remaining is None:
                remaining = parse_hms(" ".join(countdown))
        elif any("领取" in b and "积分" in b for b in buttons):
            state = "gift_dialog"
        elif any(b.strip() == "领取礼物" for b in buttons):
            state = "claimable"
        elif any("确定派出" in b for b in buttons):
            state = "scene_dialog"
        elif any("派猫猫旅行" in b for b in buttons):
            state = "idle"
        return {"state": state, "remaining_sec": remaining, "raw": joined[:1500]}

    # ---- 动作 ---------------------------------------------------------------
    @staticmethod
    def _claim_confirmed(after: dict) -> bool:
        """领奖是否完成：出现「已领取」或页面回到可派遣/旅行中，都算成功。

        用户口径（2026-11-10）：**领取后变成可派遣就是领取成功了**。
        实测：点完「领取 N 积分」后页面常直接回到 idle（派猫猫旅行），AX 树
        根本不出现「已领取」字样；只认文案会误报 FAIL。反之若页面仍停在
        claimable / gift_dialog / unknown，则不确认，避免把没领到记成成功。
        """
        info = GrowthCenter.read_state(after)
        if "已领取" in info["raw"]:
            return True
        return info["state"] in ("idle", "traveling")

    def _has_dialog(self, snap: dict) -> bool:
        return any(h in self.read_state(snap)["raw"] for h in DIALOG_HINTS)

    def dismiss_dialog(self, pid: int, wid: int) -> bool:
        """动作完成后清理页面浮层，避免遮挡后续 AX 读取。

        三级策略（2026-11-10 领奖后弹窗没关 → 派遣按钮 AXEnabled=false）：
          1. 快照有浮层特征时，点内容区精确匹配的「关闭 / 我知道了 / ×」等按钮；
          2. 找不到匹配按钮、或点了仍在 → 发 Escape（CGEventPostToPid，不移动光标）；
          3. 复核快照确认浮层已消失，返回是否真的清干净。

        只点精确匹配的 label，天然排除浏览器「关闭标签页 / 关闭窗口」，
        也不误点「确定派出」「领取 N 积分」等资源类按钮。
        """
        snap = self.snapshot(pid, wid)
        if "error" in snap:
            return False
        if not self._has_dialog(snap):
            return False

        candidates = [
            el for el in snap.get("elements", [])
            if el.get("role") == "AXButton"
            and str(el.get("label") or "").strip() in DIALOG_CLOSE_LABELS
            and (el.get("element_index") or 0) > 20
        ]
        if candidates:
            el = max(candidates, key=lambda e: e.get("element_index") or 0)
            payload: dict[str, Any] = {"pid": pid, "window_id": wid}
            if el.get("element_token"):
                payload["element_token"] = el["element_token"]
            else:
                payload["element_index"] = el.get("element_index")
            out = self._call("click", payload)
            if "error" in out or out.get("path") not in (None, "ax"):
                return False
            time.sleep(0.4)

        after = self.snapshot(pid, wid)
        if not self._has_dialog(after):
            return True

        # 兜底：Escape 关闭浮层（background 投递，不抢前台、不移动光标）
        self._call("press_key", {"pid": pid, "key": "escape"})
        time.sleep(0.4)
        return not self._has_dialog(self.snapshot(pid, wid))

    def claim_gift(self) -> ActionResult:
        win = self._window()
        if not win:
            return self._block_result("gift")
        pid, wid = win
        snap = self.snapshot(pid, wid)
        st = self.read_state(snap)["state"]

        # 弹窗已经打开：直接点「领取 N 积分」
        if st == "gift_dialog":
            ok, label = self._click(snap, lambda s: "领取" in s and "积分" in s and "已领取" not in s, pid, wid)
            if not ok:
                self.dismiss_dialog(pid, wid)
                return ActionResult(False, "gift", label)
            after = self.snapshot(pid, wid)
            confirmed = self._claim_confirmed(after)
            closed = self.dismiss_dialog(pid, wid)
            return ActionResult(confirmed, "gift", f"claimed {label}",
                                {"confirmed": confirmed, "dialog_closed": closed})

        if st != "claimable":
            return ActionResult(True, "gift", f"nothing claimable (state={st})", {"state": st})

        ok, label = self._click(snap, lambda s: s.strip() == "领取礼物", pid, wid)
        if not ok:
            self.dismiss_dialog(pid, wid)
            return ActionResult(False, "gift", label)
        dialog = self.snapshot(pid, wid)
        ok2, label2 = self._click(dialog, lambda s: "领取" in s and "积分" in s and "已领取" not in s, pid, wid)
        if not ok2:
            self.dismiss_dialog(pid, wid)
            return ActionResult(False, "gift", f"gift dialog: {label2}")
        after = self.snapshot(pid, wid)
        confirmed = self._claim_confirmed(after)
        closed = self.dismiss_dialog(pid, wid)
        return ActionResult(confirmed, "gift", f"claimed {label2}",
                            {"confirmed": confirmed, "label": label2, "dialog_closed": closed})

    @staticmethod
    def scan_exchanges(snap: dict) -> list[tuple[str, str]]:
        """只读扫描当前确实可兑的项，返回 (按钮文案, 类型)。

        保守规则：
          - 兑换奖励：连登里程碑，页面显示按钮即可兑
          - 抽奖(N)：N > 0 才兑
          - 开启盲盒 a/b：a >= b（能量满）才兑，避免空点
          - 补登卡/日期按钮：有限资源，永不自动点
        """
        found: list[tuple[str, str]] = []
        for el in snap.get("elements", []):
            if el.get("role") != "AXButton":
                continue
            lab = str(el.get("label") or "").strip()
            if lab == "兑换奖励":
                found.append((lab, "milestone"))
                continue
            m = re.search(r"抽奖\((\d+)\)", lab)
            if m and int(m.group(1)) > 0:
                found.append((lab, "lottery"))
                continue
            m = re.search(r"开启盲盒\s*(\d+)\s*/\s*(\d+)", lab)
            if m and int(m.group(1)) >= int(m.group(2)):
                found.append((lab, "blindbox"))
        return found

    @staticmethod
    def _close_dialog(snap: dict, call, pid: int, wid: int) -> bool:
        """关闭页面内的浮层（只认弹窗区域的「关闭」，不碰浏览器标签页）。"""
        for el in snap.get("elements", []):
            lab = str(el.get("label") or "").strip()
            idx = el.get("element_index") or 0
            if el.get("role") == "AXButton" and lab == "关闭" and idx > 150:
                payload: dict[str, Any] = {"pid": pid, "window_id": wid}
                if el.get("element_token"):
                    payload["element_token"] = el["element_token"]
                else:
                    payload["element_index"] = idx
                call("click", payload)
                return True
        return False

    def claim_exchanges(self, claimed_before: Callable[[str], bool] | None = None) -> list[ActionResult]:
        """自动兑换可兑项；claimed_before 由调用方提供，保证同一天每项只兑一次。"""
        win = self._window()
        if not win:
            return [self._block_result("exchange")]
        pid, wid = win
        snap = self.snapshot(pid, wid)
        # 自愈：上一轮遗留的「选择奖励」弹窗先关掉
        if "选择奖励" in self.read_state(snap)["raw"]:
            self._close_dialog(snap, self._call, pid, wid)
            snap = self.snapshot(pid, wid)
        candidates = self.scan_exchanges(snap)
        if not candidates:
            self.dismiss_dialog(pid, wid)
            return [ActionResult(True, "exchange", "nothing exchangeable", {"count": 0})]
        results: list[ActionResult] = []
        for label, kind in candidates:
            key = f"{kind}:{label}"
            if claimed_before is not None and claimed_before(key):
                results.append(ActionResult(
                    True, "exchange", f"already claimed today: {key}",
                    {"kind": kind, "label": label, "key": key, "skipped": True},
                ))
                continue
            current = self.snapshot(pid, wid)  # 每次点击前重建索引
            ok, clicked = self._click(current, lambda s, t=label: s.strip() == t, pid, wid)
            if not ok:
                results.append(ActionResult(False, "exchange", f"{kind}: {clicked}",
                                           {"kind": kind, "key": key}))
                continue
            after = self.snapshot(pid, wid)

            if kind == "milestone":
                # 「兑换奖励」会打开选择奖励弹窗；只有不含「还差/未解锁/已领取」的才是真可兑
                unlocked = [
                    str(e.get("label") or "").strip()
                    for e in after.get("elements", [])
                    if e.get("role") == "AXButton"
                    and any(k in str(e.get("label") or "") for k in ("领取", "兑换"))
                    and not any(k in str(e.get("label") or "") for k in
                                ("还差", "未解锁", "已领取", "兑换奖励"))
                ]
                if not unlocked:
                    self._close_dialog(after, self._call, pid, wid)
                    results.append(ActionResult(
                        True, "exchange", "milestone: no unlocked reward yet",
                        {"kind": kind, "key": key, "skipped": True},
                    ))
                    continue
                target = unlocked[0]
                ok2, _ = self._click(after, lambda s, t=target: s.strip() == t, pid, wid)
                final = self.snapshot(pid, wid)
                self._close_dialog(final, self._call, pid, wid)
                results.append(ActionResult(
                    ok2, "exchange", f"milestone claimed: {target}",
                    {"kind": kind, "key": key, "label": target},
                ))
                continue

            still = any(
                str(e.get("label") or "").strip() == label
                for e in after.get("elements", [])
            )
            results.append(ActionResult(
                ok and not still, "exchange", f"{kind}: {clicked}",
                {"kind": kind, "label": label, "key": key, "changed": not still},
            ))
        self.dismiss_dialog(pid, wid)
        return results

    @staticmethod
    def _exhausted_hit(raw: str) -> str | None:
        return next((m for m in DISPATCH_EXHAUSTED_MARKERS if m in (raw or "")), None)

    def dispatch(self) -> ActionResult:
        win = self._window()
        if not win:
            return self._block_result("travel_dispatch")
        pid, wid = win
        # 派遣前先清浮层（领奖确认弹窗未关会让按钮 AXEnabled=false）
        self.dismiss_dialog(pid, wid)
        snap = self.snapshot(pid, wid)
        st = self.read_state(snap)["state"]
        raw = self.read_state(snap)["raw"]
        hit = self._exhausted_hit(raw)
        if hit:
            return ActionResult(
                False, "travel_dispatch",
                f"dispatch exhausted today (page shows {hit})",
                {"dispatch_blocked": "exhausted", "evidence": hit},
            )

        if st == "idle":
            ok, label = self._click(snap, lambda s: "派猫猫旅行" in s, pid, wid)
            if not ok:
                # AXEnabled=false：多半是浮层未清或当天次数已用完。
                # 强关浮层（含 Escape）后重试一次，仍失败就判定今天不再尝试。
                if "AXEnabled" in str(label) or "refusing AXPress" in str(label):
                    self._call("press_key", {"pid": pid, "key": "escape"})
                    time.sleep(1.0)
                    snap = self.snapshot(pid, wid)
                    info2 = self.read_state(snap)
                    hit = self._exhausted_hit(info2["raw"])
                    if hit:
                        return ActionResult(
                            False, "travel_dispatch",
                            f"dispatch exhausted today (page shows {hit})",
                            {"dispatch_blocked": "exhausted", "evidence": hit},
                        )
                    if info2["state"] == "idle":
                        ok, label = self._click(
                            snap, lambda s: "派猫猫旅行" in s, pid, wid)
                        if ok:
                            snap = self.snapshot(pid, wid)
                            st = self.read_state(snap)["state"]
                    if not ok:
                        return ActionResult(
                            False, "travel_dispatch", label,
                            {"dispatch_blocked": "exhausted"},
                        )
                else:
                    return ActionResult(False, "travel_dispatch", label)

        if st == "scene_dialog":
            if self.scene:
                self._click(snap, lambda s: "切换到" in s and self.scene in s, pid, wid)
                snap = self.snapshot(pid, wid)
            ok, label = self._click(snap, lambda s: "确定派出" in s, pid, wid)
            if not ok:
                blocked = "exhausted" if ("AXEnabled" in str(label)
                                          or "refusing AXPress" in str(label)) else None
                return ActionResult(False, "travel_dispatch", label,
                                    {"dispatch_blocked": blocked} if blocked else {})

        after = self.snapshot(pid, wid)
        info = self.read_state(after)
        if info["state"] != "traveling" or info["remaining_sec"] is None:
            self.dismiss_dialog(pid, wid)
            hit = self._exhausted_hit(info["raw"])
            return ActionResult(
                False, "travel_dispatch",
                f"dispatch not confirmed (state={info['state']})",
                {"state": info["state"],
                 "dispatch_blocked": "exhausted" if hit else None},
            )
        due = time.time() + info["remaining_sec"] + self.claim_offset_sec
        closed = self.dismiss_dialog(pid, wid)
        return ActionResult(
            True, "travel_dispatch",
            f"dispatched; real duration {info['remaining_sec']}s",
            {"remaining_sec": info["remaining_sec"], "due_at": due,
             "scene": self.scene or "default", "dialog_closed": closed},
        )

    def tick(self, force: bool = False) -> ActionResult:
        """一轮：旅行中->记录真实到期；可领->领奖并再派遣；空闲->派遣。"""
        win = self._window()
        if not win:
            return self._block_result("travel")
        pid, wid = win
        # 先清掉上一轮遗留的「已领取 / 选择奖励」等浮层，否则会遮挡本轮状态读取
        # （实测 2026-11-10 11:19 重启轮：残留弹窗导致整轮误判 gift_dialog 失败）
        self.dismiss_dialog(pid, wid)
        snap = self.snapshot(pid, wid)
        info = self.read_state(snap)
        state = info["state"]

        if state == "traveling" and not force:
            if info["remaining_sec"] is None:
                return ActionResult(False, "travel", "countdown unreadable", info)
            due = time.time() + info["remaining_sec"] + self.claim_offset_sec
            return ActionResult(
                True, "travel", f"traveling; due from real countdown ({info['remaining_sec']}s)",
                {"state": state, "due_at": due, "remaining_sec": info["remaining_sec"]},
            )

        if state in ("claimable", "gift_dialog") or force:
            claimed = self.claim_gift()
            dispatched = self.dispatch()
            due = dispatched.data.get("due_at")
            ok = dispatched.ok and (claimed.ok or claimed.data.get("state") == "traveling")
            data: dict[str, Any] = {
                "state": state, "due_at": due,
                "claim": claimed.as_dict(), "dispatch": dispatched.as_dict(),
            }
            if dispatched.data.get("dispatch_blocked"):
                data["dispatch_blocked"] = dispatched.data["dispatch_blocked"]
            return ActionResult(ok, "travel",
                                f"claim={claimed.detail}; dispatch={dispatched.detail}", data)

        if state == "idle":
            dispatched = self.dispatch()
            data = {"state": state, "due_at": dispatched.data.get("due_at"),
                    "dispatch": dispatched.as_dict()}
            if dispatched.data.get("dispatch_blocked"):
                data["dispatch_blocked"] = dispatched.data["dispatch_blocked"]
            return ActionResult(
                dispatched.ok, "travel", dispatched.detail, data,
            )

        return ActionResult(False, "travel", f"unknown growth-center state: {state}", info)
