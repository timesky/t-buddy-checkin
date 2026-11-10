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

# 动作完成后需要清掉的页面浮层：只在快照出现这些提示/按钮时才点关闭，
# 避免误关浏览器标签页或点到「确定派出」等资源类按钮。
DIALOG_HINTS = ("已领取", "领取成功", "恭喜", "选择奖励", "我知道了", "关闭")
DIALOG_CLOSE_LABELS = ("关闭", "我知道了", "知道了", "好的", "完成", "确定")


def parse_hms(text: str) -> int | None:
    """从任意文本里抽 HH:MM:SS 并返回总秒数。"""
    m = _COUNTDOWN.search(text or "")
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


class GrowthCenter:
    def __init__(self, cfg: dict, runner: Callable[..., Any] | None = None) -> None:
        growth = cfg.get("growth", {})
        self.bin = growth.get("cua_driver_bin") or CUA_DRIVER_BIN
        # 默认 auto：探测任意浏览器里已打开的成长中心窗口，不限定 Edge
        self.browser_app = growth.get("browser_app", "auto") or "auto"
        self.title_hint = growth.get("title_hint", "个人中心")
        self.claim_offset_sec = float(growth.get("claim_offset_sec", 3))
        self.scene = growth.get("scene", "")  # 留空 = 用页面默认场景
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

        强成长内容（派猫猫旅行 / 领取礼物 / 旅行倒计时 等真实按钮）**优先**：
        已登录的成长中心页面可能夹杂「请登录」之类的泛化文案（实测侧栏就有），
        不能因此误判为未登录。只有页面没有成长内容、且出现登录特征时才算登录页。
        """
        if not isinstance(snap, dict):
            return "other"
        labels = " ".join(str(e.get("label") or "") for e in snap.get("elements", []))
        text = f"{snap.get('raw', '')} {labels}"
        if any(m in text for m in GROWTH_CONTENT_MARKERS):
            return "growth"
        if any(m in text for m in LOGIN_MARKERS):
            return "login"
        return "other"

    def _window(self) -> tuple[int, int] | None:
        """找已打开的成长中心窗口（任意浏览器），返回 (pid, window_id)。

        不限定浏览器：browser_app="auto" 时扫描所有可见窗口，按页面内容（而非
        app 名）判定是否为成长中心；命中登录页则记为 login_required 并提示用户登录。
        """
        self._block = None
        data = self._call("list_windows", {})
        wins = data.get("windows", [])
        if self.browser_app != "auto":
            # 用户显式指定浏览器时才按 app 过滤，否则任意浏览器皆可
            wins = [w for w in wins if w.get("app_name") == self.browser_app]
        wins = [w for w in wins if (w.get("bounds") or {}).get("width", 0) > 400]
        if not wins:
            self._block = ("not_found",
                           "未找到任何浏览器窗口：请从 WorkBuddy 点开「个人中心」"
                           "(www.workbuddy.cn/profile/growth-center) 再运行")
            return None
        # 粗筛排序：标题含 title_hint 或 WorkBuddy 的优先，其次按窗口面积
        title_hint = self.title_hint
        ranked = sorted(
            wins,
            key=lambda w: (
                (title_hint in str(w.get("title") or "")
                 or "workbuddy" in str(w.get("title") or "").lower()),
                w["bounds"]["width"] * w["bounds"]["height"],
            ),
            reverse=True,
        )
        # 逐个用 AX 内容精筛（最多前 4 个候选），同时识别登录页
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

    def _click(self, snap: dict, predicate, pid: int, wid: int) -> tuple[bool, str]:
        for el in snap.get("elements", []):
            if el.get("role") != "AXButton":
                continue
            label = str(el.get("label") or "")
            if predicate(label):
                idx = el.get("element_index")
                if idx is None:
                    continue
                out = self._call("click", {"pid": pid, "window_id": wid, "element_index": idx})
                if "error" in out:
                    return False, out["error"]
                return True, label
        return False, "button not found"

    # ---- 状态解析 -----------------------------------------------------------
    @staticmethod
    def read_state(snap: dict) -> dict[str, Any]:
        """从 AX 快照读真实旅行状态与剩余秒数。"""
        labels: list[str] = []
        for el in snap.get("elements", []):
            lab = str(el.get("label") or "")
            if lab and lab != "None":
                labels.append(lab)
        joined = " | ".join(labels[:250])
        state = "unknown"
        remaining = None
        if "旅行倒计时" in joined:
            state = "traveling"
            for lab in labels:
                if "旅行倒计时" in lab:
                    remaining = parse_hms(lab)
                    if remaining is not None:
                        break
            if remaining is None:
                remaining = parse_hms(joined)
        elif any("领取" in lab and "积分" in lab for lab in labels):
            state = "gift_dialog"
        elif any(lab.strip() == "领取礼物" for lab in labels):
            state = "claimable"
        elif any("确定派出" in lab for lab in labels):
            state = "scene_dialog"
        elif any("派猫猫旅行" in lab for lab in labels):
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

    def dismiss_dialog(self, pid: int, wid: int) -> bool:
        """动作完成后清理页面浮层，避免弹窗遮挡后续 AX 读取。

        保守规则：快照里必须出现明确的弹窗特征才动手；只点页面内容区里的
        「关闭 / 我知道了 / 好的」等按钮（element_index > 20 排除浏览器工具栏），
        精确匹配 label —— 「确定派出」「领取 N 积分」等绝不会被误点。
        """
        snap = self.snapshot(pid, wid)
        if "error" in snap:
            return False
        raw = self.read_state(snap)["raw"]
        if not any(hint in raw for hint in DIALOG_HINTS):
            return False
        candidates = [
            el for el in snap.get("elements", [])
            if el.get("role") == "AXButton"
            and str(el.get("label") or "").strip() in DIALOG_CLOSE_LABELS
            and (el.get("element_index") or 0) > 20
        ]
        if not candidates:
            return False
        el = max(candidates, key=lambda e: e.get("element_index") or 0)
        out = self._call("click", {
            "pid": pid, "window_id": wid, "element_index": el["element_index"],
        })
        return "error" not in out

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
                call("click", {"pid": pid, "window_id": wid, "element_index": idx})
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

    def dispatch(self) -> ActionResult:
        win = self._window()
        if not win:
            return self._block_result("travel_dispatch")
        pid, wid = win
        snap = self.snapshot(pid, wid)
        st = self.read_state(snap)["state"]

        if st == "idle":
            ok, label = self._click(snap, lambda s: "派猫猫旅行" in s, pid, wid)
            if not ok:
                return ActionResult(False, "travel_dispatch", label)
            snap = self.snapshot(pid, wid)
            st = self.read_state(snap)["state"]

        if st == "scene_dialog":
            if self.scene:
                self._click(snap, lambda s: "切换到" in s and self.scene in s, pid, wid)
                snap = self.snapshot(pid, wid)
            ok, label = self._click(snap, lambda s: "确定派出" in s, pid, wid)
            if not ok:
                return ActionResult(False, "travel_dispatch", label)

        after = self.snapshot(pid, wid)
        info = self.read_state(after)
        if info["state"] != "traveling" or info["remaining_sec"] is None:
            self.dismiss_dialog(pid, wid)
            return ActionResult(
                False, "travel_dispatch",
                f"dispatch not confirmed (state={info['state']})",
                {"state": info["state"]},
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
            return ActionResult(
                ok, "travel",
                f"claim={claimed.detail}; dispatch={dispatched.detail}",
                {"state": state, "due_at": due, "claim": claimed.as_dict(), "dispatch": dispatched.as_dict()},
            )

        if state == "idle":
            dispatched = self.dispatch()
            return ActionResult(
                dispatched.ok, "travel", dispatched.detail,
                {"state": state, "due_at": dispatched.data.get("due_at"),
                 "dispatch": dispatched.as_dict()},
            )

        return ActionResult(False, "travel", f"unknown growth-center state: {state}", info)
