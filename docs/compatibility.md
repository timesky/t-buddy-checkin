<style>
  :root { --wb-max-width: 980px; }
  body { max-width: var(--wb-max-width); margin: 0 auto; padding: 0 24px 48px; }
  pre, table { max-width: 100%; overflow-x: auto; }
  img { max-width: 100%; height: auto; }
</style>

# 兼容性：版本不对，后面全白做

[← 文档索引](README.md) · [主 README](../README.md)

本技能操作的是 **WorkBuddy 客户端内嵌的成长中心页面**，不是网页。5.11 之前成长中心
还是**网页版活动中心**，客户端里根本没有这个页面 —— 所以有硬性版本红线，且**不做降级兼容**。

## 1. 版本矩阵

| 客户端版本 | 结论 | 依据 |
|---|---|---|
| **≥ 5.11.0** | ✅ 支持，实测基线 **5.11.3** | 客户端内置成长中心 + 左下角「Buddy加油站」浮层，五个任务齐全 |
| 5.10.x | ❌ 不支持 | 官方更新日志里没有公开的 5.10，也未实测，按不支持处理 |
| **≤ 5.9.x** | ❌ 不支持 | 成长中心还是**网页版活动中心**：CDP 的 target 列表里看不到 `type=iframe` 的 growth-center，也没有「Buddy加油站」浮层。两条通道都不存在 |

**预览 / 灰度版**（版本号带 `-preview`、`-beta`、`-rc` 等后缀）：只要 `major.minor ≥ 5.11`
就放行，`compat` 只把通道标成 `preview` 写进日志，不做限制。

**比实测基线更新**（如 5.12+）：同样放行，但会提示先跑 `./run.sh inspect` 复核 DOM ——
成长中心是远端下发的页面，结构可能会变。

### 版本号怎么查（三选一，都不会改东西）

```bash
# ① 最快：技能自己报，同时校验 5.11 红线
./run.sh deps check

# ② 命令行读安装包（装在别处就换路径，或先 export WORKBUDDY_APP=...）
defaults read /Applications/WorkBuddy.app/Contents/Info.plist CFBundleShortVersionString

# ③ 界面：客户端左下角头像，菜单里带当前版本号
```

版本号长这样：`5.11.3.43806934-ecace0f7` —— 第三段之后是构建号与 commit，不参与比较。

## 2. 升级到 5.11+

> **这是新装唯一的卡点**：官方更新日志里 **5.11.0 发布于 2026-11-03，标注「灰度中」**
> （<https://www.workbuddy.cn/docs/workbuddy/Changelog>），所以官网下载页和「历史版本下载」
> 页最新**只到 5.9.3** —— 照着官网下载，拿到的正是跑不起来的那一版。

### 方式一：客户端内升级（推荐先试）

```
客户端左下角头像 → 检查更新
```

灰度按账号放开，可能提示「已是最新」。提示已是最新但仍显示 5.9.x，说明你不在灰度名单里，
用方式二。

### 方式二：官方安装包覆盖安装

链接为官方 CDN，**2026-11-15 实测 HTTP 200、`application/x-apple-diskimage`**，可直接装。

| 芯片 | 下载地址 | 体积 |
|---|---|---|
| Apple 芯片（M 系列） | <https://download.codebuddy.cn/workbuddy/saas/darwin-arm64/WorkBuddy-darwin-arm64-5.11.3.43806934-ecace0f7.dmg> | 约 358 MB |
| Intel | <https://download.codebuddy.cn/workbuddy/saas/darwin-x64/WorkBuddy-darwin-x64-5.11.3.43806934-ecace0f7.dmg> | 约 390 MB |

查芯片：屏幕左上角 苹果菜单 → 「关于本机」 → 「芯片」。写 `Apple M*` 就是 ARM64，写 `Intel` 就是 x64。

装的时候有三个坑（前两条是官方安装指南原话）：

1. **必须拖进「应用程序」** —— 双击 .dmg 后直接从磁盘映像里运行 WorkBuddy，后续无法自动更新。
2. 从磁盘映像里运行时若提示无法自动更新，按提示点「移动到应用程序」。
3. **先退出旧版再启动**：`⌘Q`，并确认菜单栏图标也退干净，否则新版本不会接管。

装完回客户端左下角头像核对版本号 ≥ 5.11.0，再跑 `./run.sh deps check`。

> 官方安装指南：<https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Installation-Mac-Guide>

## 3. 其余硬依赖

缺一条就跑不通，装之前先对一遍。

| 前置 | 要求 | 不满足会怎样 |
|---|---|---|
| 操作系统 | **macOS 12 (Monterey) 及以上**（WorkBuddy 官方要求） | 客户端本身起不来 |
| 平台 | **仅 macOS**。用了 `launchd` / `launchctl` / `lsof` / `defaults` / `pgrep` / `osascript` | Windows / Linux 无法使用，别装 |
| WorkBuddy 安装位置 | 默认 `/Applications/WorkBuddy.app`；装在别处需 `WORKBUDDY_APP=/path/WorkBuddy.app` | 版本读不到，日志里客户端显示「未运行」 |
| 客户端登录态 | 已登录，且**主窗口存在**（可最小化，不能关窗） | 成长中心在登录态里；没有窗口就没有可连的 webContents |
| 界面语言 | **简体中文** | 50+ 处按中文文案定位（「成长计划」「立即领取」「兑换」「使用」「抽奖(N)」…），只有成长中心入口可配 `[growth].open_labels`，其余换英文就点不到 |
| Python | **≥ 3.11** | `tomllib` / `sqlite3` / `zoneinfo` 缺，`run.sh` 直接退出 |
| Node.js | **≥ 22** | `tools/cdp_client.mjs` 用全局 `WebSocket`，`agent-browser` 自身也声明 `engines.node >= 22`；20/21 上连不上 iframe |
| agent-browser | `0.42.x`（`cd tools && npm install`） | 打不开成长中心页面 |
| CDP 调试端口 | 客户端带 `WORKBUDDY_REMOTE_DEBUGGING_PORT` 启动 | `deps check` 报 `WorkBuddy CDP MISSING` |
| 网络 | 能访问 `workbuddy.cn`（成长中心是服务端数据）；`npm install` 需 registry | 签到状态读不到 / 装不上依赖 |
| 可写目录 | `~/.local/share/buddy-web-checkin/`、`~/Library/LaunchAgents/` | 账本与登录项写不进去 |
| 权限 | **不需要 sudo**，也不需要辅助功能/录屏授权（可选件 `agent-desktop` 才需要） | — |

## 4. 版本红线是自动的

不是写在文档里让你自己记，而是代码里拦：

- `./run.sh deps check` **第一项**就是客户端版本，不够时直接把上面两条升级方式打出来。
- `checkin-now` / `makeup-now` / `exchange-now` / `lottery-now` / `travel-now` / `once` /
  `verify` / `daemon` 在版本过低时**动手前就退出（退出码 `3`）**，不会白跑一天。
- `ports status` / `inspect` 会打印客户端版本、安装路径与放行判定。

实现见 [`src/buddy_checkin/compat.py`](../src/buddy_checkin/compat.py)：版本解析（吃得下
`5.11.3.43806934-ecace0f7`、`5.12.0-preview.1`）、放行判定、官方直链、升级文案都只有一份，
改版本红线只改这个文件。
