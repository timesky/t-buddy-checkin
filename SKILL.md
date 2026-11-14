---
name: buddy-web-checkin
description: 独立运行 WorkBuddy 每日活跃（签到）校验、签到礼物自动兑换、派猫猫旅行自动派遣，并在旅行精确到期时领取礼物后立刻再次派遣。全部通过 CDP 直连 WorkBuddy 客户端内嵌的成长中心页面，DOM 级操作，全程后台隔离、不抢焦点、不干扰人工操作。触发词：帮我签到、领今日礼包、自动签到、签到礼物兑换、派猫猫旅行、自动派遣、领取旅行礼物、旅行到期领奖、签到守护进程。
---

# buddy-web-checkin

这是一个**可作为独立服务执行的 CLI 技能**：运行时不依赖 WorkBuddy agent / 模型 / 在线 API，调度器是自带的标准库 `heapq`，状态在 SQLite，由 macOS LaunchAgent 直接拉起常驻进程。

**所有自动化操作都走 CDP**：`agent-browser` 打开客户端侧栏里的成长中心，`tools/cdp_client.mjs` 直连成长中心 iframe 做 DOM 级读取与点击。旧的 CuaDriver / AX 通道已整体移除（`adapters/desktop.py`、`adapters/growth.py` 已删除），`agent-desktop` 仅作可选兜底、不参与日常流程。

操作对象依赖 WorkBuddy：成长中心是客户端内嵌页面，客户端需带调试端口启动（`scripts/launch-workbuddy-debug.sh` 一键完成）。

> `BUDDY_FREE_MODEL=1` 是按 WorkBuddy 技能设计时的运维确认闸门，**独立服务下不消耗额度，但按约定保留**：除 `model-check` / `deps` / `install-launchd` / `inspect` 纯本地运维外，所有命令都要该环境变量。LaunchAgent plist 已内置。

## 安装第三方依赖（安装 / 重新拉取技能时必跑）

```bash
cd tools && npm install && cd ..    # agent-browser（必需）+ agent-desktop（可选）
./run.sh deps check                 # 五项依赖：Python / node / agent-browser / WorkBuddy CDP / agent-desktop
```

| 依赖 | 必需 | 说明 |
|---|---|---|
| Python ≥ 3.11 | ✅ | 标准库 tomllib / sqlite3 / zoneinfo |
| node ≥ 20 | ✅ | 跑 `tools/cdp_client.mjs`（CDP 桥） |
| agent-browser | ✅ | 跨 webContents 聚合快照/点击，打开成长中心 |
| WorkBuddy CDP 端口 | ✅ | `launchctl setenv WORKBUDDY_REMOTE_DEBUGGING_PORT 9222` + 重启客户端 |
| agent-desktop | 可选 | AX 兜底，需给启动它的 App 授权辅助功能 |

CDP 端口是全新安装最容易漏的一步：客户端**不认** `--remote-debugging-port` 启动参数，只认环境变量。跑 `scripts/launch-workbuddy-debug.sh` 会自动 setenv → 优雅退出 → 重启 → 探测端口 → 列出 targets。

## 用法

手动执行：`BUDDY_FREE_MODEL=1 ./run.sh <command>`。

| 命令 | 作用 | 是否需要人工 |
|---|---|---|
| `deps check` | 逐项检查依赖（安装后必跑） | 否 |
| `inspect` | 只读诊断：CDP 连通 / targets / 浮层签到状态 / 页面按钮（跨机器排查首选） | 否 |
| `status` | 三条时间线下次执行时间 / 最近运行 | 否 |
| `checkin-now` | 签到线一轮（点左下角浮层「立即领取」） | 否 |
| `exchange-now` | 兑换检查线一轮 | 否 |
| `travel-now` | 旅行线一轮（领礼物→派遣→读真实倒计时） | 否 |
| `once` | 全量一轮 | 否 |
| `daemon` | 统一常驻进程：三条独立时间线 | 否 |
| `install-launchd` | 写 LaunchAgent plist（RunAtLoad+KeepAlive） | 加载命令由你确认 |
| `model-check` | 查看免费模型策略与确认状态 | 否 |

## 签到语义（2026-11-14 更正，重要）

**当日签到 = 点主窗口左下角「Buddy加油站」浮层的「立即领取」**。之前「已无签到
按钮、签到 = 读活跃地图」的结论是错的：卡片是远端运营位下发的 HTML（`app.asar`
里搜不到），且活跃地图只代表今天用过客户端，与签到领积分无关 —— 那版实现从来没
真正签到过，却天天报 `confirmed=True`。

- 卡片在 `div.daily-checkin` 内 `.wb-slot--avatar-top-combo` 的 **shadow DOM**，
  light DOM 永远为空（读 textContent 会误判浮层不存在）
- 签到按钮 `#go` + `data-track-props={"type":"checkin"}`；成功后 `#tt` 出现
  「领取成功」，签到卡从轮播里消失
- 只有「看到领取成功 / 签到卡消失」才写 `last_checkin_day`；浮层不在 = 未知，
  下一轮重试（避免整天假阳性）
- 漏签只能去成长中心日历消耗**补登卡**补，本技能不自动消耗
- `.gs-cal-cell.is-today` 实心绿只代表「今天有活跃记录」，仅作参考

## 三条时间线

进程启动时**三条线都立即执行一次**，之后各自向后滚动：

| 线 | 周期 | 停止条件 |
|---|---|---|
| `checkin` | 每天 0 点恢复，每小时检查 | 确认已打卡 → 禁用到当天 23:59 |
| `exchange` | 每天 0 点恢复，每小时检查 | 兑换成功 **或** 确认「本月已兑」→ 禁用到 23:59 |
| `travel` | 按页面真实倒计时滚动（唯一动态） | 不停，领奖→再派遣→读新倒计时 |

**派遣时长每次不同**：页面只提示 1～4 小时，必须派遣后重读「旅行倒计时 HH:MM:SS」算 `due_at`，禁止写死间隔。

**派遣次数用尽**：页面出现「明天再来 / 今日已派完 / 次数已用完」→ `dispatch_blocked=exhausted`，旅行线禁用到 23:00（已过则次日 0 点）；标记落库，重启跳过实际检查，跨天自动清除。

## 执行约束

- **不抢焦点**：所有操作走 CDP 协议（DOM 级 click），不激活窗口、不移动光标、不合成物理输入
- **状态只认结构**：用 `.gs-cal-cell` 类名、`button` 的 disabled、`旅行倒计时` 文本判定；不靠模糊文案猜测
- **兑换保守**：`兑换奖励` 只点弹窗里已解锁档位，`抽奖(N)` 仅 N>0，`开启盲盒 a/b` 仅能量满；补登卡与日期按钮永不自动消耗
- **不做任务**：`去完成` 一类任务按钮永不点击
- **用完收界面**：任务结束（签到校验 / 领取 / 派遣 / 兑换）后关闭成长中心弹窗；只关本次由工具打开的，用户自己开着的页面保持原样、不抢。`close_page_after_action=false` 可关闭该行为
- **幂等**：所有领取经 SQLite 唯一键去重，重复运行不重复领
- **失败不换通道**：CDP 不可达时明确报错并给修复命令，不静默降级
- **不自动登录**：不填账号密码、不绕验证码
- **时间口径**：一律北京时间；naive 时间按北京时间解释
- **凭据**：输出脱敏，不读取或导出客户端登录态与令牌

## 跨机器排查

另一台机器失败时按顺序跑：`./run.sh deps check` → `./run.sh inspect` → 对照 README 故障排查表。入口文案不同改 `[growth].open_labels`（如 `["成长计划"]`）。

详细架构与故障排查见 `README.md`。

## 免费模型约束（历史约定，独立服务下不耗额度）

1. 手动执行前以 `BUDDY_FREE_MODEL=1` 显式确认，否则命令拒绝执行（退出码 2）。
2. `install-launchd` 生成的 plist 已内置 `BUDDY_FREE_MODEL=1`。
3. 查看策略：`./run.sh model-check`（纯本地运维）。

## Git 管理

仓库 `https://github.com/timesky/t-buddy-checkin`（`main`）。`.gitignore` 排除 `config.toml`、`tools/node_modules/`、`*.png`、`*.sqlite3`、`last-checkin-result.json` 等本地产物。
