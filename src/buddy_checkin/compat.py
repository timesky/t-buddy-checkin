"""WorkBuddy 客户端兼容性：版本红线 / 官方下载地址 / 升级指引。

为什么单独一个模块：这是**装得上但一定跑不起来**的那类前置条件，必须能被
`deps check`、`inspect`、`ports status` 和每日调度共同引用，措辞只能有一份。

事实来源（2026-11-15 核对官方文档 + 本机实测）
------------------------------------------------
1. 官方更新日志（`/docs/workbuddy/Changelog`）：**5.11.0 发布于 2026-11-03，
   条目上明确标注「灰度中」**；「历史版本下载」页（`Download-History`）
   最新只列到 **5.9.3**。
2. 因此「照官网下载」拿到的是 5.9.x —— 那还是**网页版活动中心**：
   客户端里没有内嵌成长中心（CDP 里看不到 `type=iframe` 的 growth-center
   target），也没有主窗口左下角的「Buddy加油站」浮层。本技能的两条通道
   （浮层签到 / 成长中心 iframe）都不存在，**明确不支持**，不要试图兼容。
3. 5.11.x 起客户端内置成长中心 + 浮层，五件套（签到/兑换/补签/抽奖/派遣）
   才齐备。本项目实测基线 **5.11.3**。
4. macOS 系统要求来自官方《Mac 系统安装指南》：**macOS 12 (Monterey) 及以上**，
   不满足则 WorkBuddy 本身无法启动。

升级入口（官方）：客户端**左下角头像 → 检查更新**；拿不到灰度包时用官方
CDN 直链下载 `.dmg` 覆盖安装（必须装进「应用程序」，从磁盘映像里直接跑
无法自动更新——官方安装指南原话）。
"""

from __future__ import annotations

import os
import platform
import re

# 支持的最低客户端版本：5.11.0 起才有客户端内嵌成长中心 + 左下角浮层
MIN_VERSION = (5, 11, 0)
MIN_VERSION_TEXT = "5.11.0"

# 本项目实测基线（DOM 结构按这版写的）
TESTED_VERSION = "5.11.3"

# 明确不支持的最后一档：5.9.x 及更早 = 网页版活动中心
LAST_UNSUPPORTED_TEXT = "5.9.x"

# 官方「历史版本下载」页：公开渠道最新只到 5.9.3，仅作「怎么查版本」参考，
# 不要拿它当下载入口给新人。
DOWNLOAD_PAGE = "https://www.workbuddy.cn/docs/workbuddy/Download-History"
CHANGELOG_PAGE = "https://www.workbuddy.cn/docs/workbuddy/Changelog"
MAC_INSTALL_GUIDE = "https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Installation-Mac-Guide"

# 官方 CDN 直链（同一灰度构建，2026-11-15 实测 HTTP 200 +
# Content-Type: application/x-apple-diskimage，可直接覆盖安装）
ARTIFACTS = {
    "darwin-arm64": {
        "label": "Apple 芯片（M 系列）",
        "url": (
            "https://download.codebuddy.cn/workbuddy/saas/darwin-arm64/"
            "WorkBuddy-darwin-arm64-5.11.3.43806934-ecace0f7.dmg"
        ),
        "size": "约 358 MB",
    },
    "darwin-x64": {
        "label": "Intel 芯片",
        "url": (
            "https://download.codebuddy.cn/workbuddy/saas/darwin-x64/"
            "WorkBuddy-darwin-x64-5.11.3.43806934-ecace0f7.dmg"
        ),
        "size": "约 390 MB",
    },
}

_VERSION_RE = re.compile(r"^[vV]?(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_PREVIEW_RE = re.compile(r"(preview|beta|alpha|rc|insider|canary)", re.I)


def parse(text: str | None) -> tuple[int, int, int] | None:
    """从任意版本串里取 (major, minor, patch)。

    兼容 `5.11.3`、`v5.11.3`、`5.11.3.43806934-ecace0f7`、
    `5.12.0-preview.1`；取不到返回 None（调用方按「未知」处理，不当过期）。
    """
    if not text:
        return None
    m = _VERSION_RE.match(str(text).strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def build_no(text: str | None) -> str | None:
    """`5.11.3.43806934-ecace0f7` → `43806934`（构建号，排障时有用）。"""
    if not text:
        return None
    parts = str(text).strip().lstrip("vV").split("-")[0].split(".")
    return parts[3] if len(parts) > 3 and parts[3].isdigit() else None


def channel(text: str | None) -> str:
    """灰度/预览通道标记：只用于提示，不参与放行判定。"""
    raw = str(text or "")
    if _PREVIEW_RE.search(raw):
        return "preview"
    return "stable"


def check(version: str | None) -> dict:
    """判定客户端版本能否运行本技能。

    返回 dict：
      ok      是否放行（只有「能证明过期」才拦，未知一律放行并提示）
      level   ok / too_old / unknown / newer_than_tested
      message 给人和给 agent 看的同一句话
      action  需要人工做的事（空串表示无需动作）
    """
    parsed = parse(version)
    if parsed is None:
        return {
            "ok": True,
            "level": "unknown",
            "version": version,
            "parsed": None,
            "build": build_no(version),
            "channel": channel(version),
            "min": MIN_VERSION_TEXT,
            "tested": TESTED_VERSION,
            "message": (
                "无法识别 WorkBuddy 版本（可能没装、装在非默认路径，或客户端异常）。"
                f"本技能要求 {MIN_VERSION_TEXT}+（实测 {TESTED_VERSION}），请自行确认"
            ),
            "action": "在客户端左下角头像菜单里核对版本号；装在非 /Applications 时设 WORKBUDDY_APP 指向实际 .app",
        }
    pretty = ".".join(str(x) for x in parsed)
    base = {
        "version": version,
        "parsed": pretty,
        "build": build_no(version),
        "channel": channel(version),
        "min": MIN_VERSION_TEXT,
        "tested": TESTED_VERSION,
    }
    if parsed < MIN_VERSION:
        return {
            **base,
            "ok": False,
            "level": "too_old",
            "message": (
                f"客户端 {pretty} 太旧：{LAST_UNSUPPORTED_TEXT} 及更早的成长中心是"
                f"「网页版活动中心」，客户端里没有内嵌成长中心，也没有「Buddy加油站」"
                f"浮层 —— 本技能无法工作。需要 {MIN_VERSION_TEXT}+（实测 {TESTED_VERSION}）"
            ),
            "action": "升级 WorkBuddy 后重跑 ./run.sh deps check",
        }
    if parsed > parse(TESTED_VERSION):
        return {
            **base,
            "ok": True,
            "level": "newer_than_tested",
            "message": (
                f"客户端 {pretty} 比实测基线 {TESTED_VERSION} 新，结构可能有变动；"
                "先跑 ./run.sh inspect 看浮层与成长中心是否正常"
            ),
            "action": "",
        }
    return {
        **base,
        "ok": True,
        "level": "ok",
        "message": f"客户端 {pretty} 在支持范围内（≥{MIN_VERSION_TEXT}，实测基线 {TESTED_VERSION}）",
        "action": "",
    }


def machine_arch() -> str:
    """本机该下哪个包：darwin-arm64 / darwin-x64。"""
    return "darwin-arm64" if platform.machine() in ("arm64", "aarch64") else "darwin-x64"


def recommended(cfg: dict | None = None) -> dict:
    """推荐下载项；允许用环境变量覆盖（架构判断异常时兜底）。"""
    key = (os.environ.get("WORKBUDDY_ARCH") or "").strip()
    if key not in ARTIFACTS:
        key = machine_arch()
    item = dict(ARTIFACTS[key])
    item["arch"] = key
    return item


def upgrade_text(version: str | None = None, arch: str | None = None) -> str:
    """升级到 5.11+ 的完整指引（deps / inspect / 报错时原样打印）。"""
    rec = recommended()
    if arch and arch in ARTIFACTS:
        rec = {**ARTIFACTS[arch], "arch": arch}
    cur = f"当前：{version}" if version else "当前：未识别"
    lines = [
        f"== 需要升级 WorkBuddy 到 {MIN_VERSION_TEXT}+（{cur}）==",
        "",
        "为什么：5.9.x 及更早的成长中心是网页版活动中心，客户端里没有内嵌成长中心",
        "和「Buddy加油站」浮层，本技能两条通道都不存在，无法降级兼容。",
        f"（官方更新日志：5.11.0 发布于 2026-11-03，当前标注「灰度中」{CHANGELOG_PAGE}）",
        "",
        "方式一：客户端内升级（灰度按账号放开，可能提示已是最新）",
        "  左下角头像 → 检查更新",
        "",
        "方式二：官方安装包覆盖安装（拿不到灰度包时用这条，2026-11-15 验证可下载）",
        f"  {rec['label']}（{rec['size']}）：",
        f"    {rec['url']}",
        "  1) 下载 .dmg → 双击 → 把 WorkBuddy 拖进「应用程序」（从磁盘映像直接跑无法自动更新）",
        "  2) 退出旧版（⌘Q，确认菜单栏图标也退出）后再启动",
        "  3) 左下角头像 核对版本号 ≥ 5.11.0",
        f"  官方安装指南：{MAC_INSTALL_GUIDE}",
        "",
        f"注意：官方「历史版本下载」页 {DOWNLOAD_PAGE}",
        f"      公开渠道最新只到 5.9.3（即 {LAST_UNSUPPORTED_TEXT}），那个版本装了也跑不起来。",
        "",
        "升级完成后重跑：./run.sh deps check && ./run.sh inspect",
    ]
    return "\n".join(lines)


def one_line(version: str | None) -> str:
    """一行状态，用于 ports status / deps 列表。"""
    r = check(version)
    mark = {"ok": "✓", "too_old": "✗", "unknown": "?", "newer_than_tested": "✓"}[r["level"]]
    return f"{mark} {r['parsed'] or version or '未知'}（要求 ≥{MIN_VERSION_TEXT}）"
