---
name: buddy-web-checkin
description: 独立运行 WorkBuddy 每日签到、签到礼物自动兑换、派猫猫旅行自动派遣，并在旅行精确到期时领取礼物后立刻再次派遣。仅签到依赖桌面客户端，其余走已登录浏览器里的成长中心页面；全程后台隔离，不干扰人工操作。触发词：帮我签到、领今日礼包、自动签到、签到礼物兑换、派猫猫旅行、自动派遣、领取旅行礼物、旅行到期领奖、签到守护进程。
---

# buddy-web-checkin

这是一个**可作为独立服务执行的 CLI 技能**：运行时不依赖 WorkBuddy agent / 模型 / 在线 API，调度器是自带的标准库 `heapq`，状态在 SQLite，由 macOS LaunchAgent 直接拉起常驻进程。**所有自动化操作（桌面签到 + 成长中心领礼物/派遣/兑换）都走 CuaDriver 无障碍树（AX 通道，已授权）**——它是唯一的外部工具与实际后端；headless 浏览器（agent-browser / agent-desktop / CDP / computer-use）均已移除。

「被操作的对象」依赖 WorkBuddy：签到要点 WorkBuddy 桌面客户端窗口，成长中心是 WorkBuddy 个人中心网页（任意浏览器、需已登录）。这两者要开着，但不要求 agent 环境。

> `BUDDY_FREE_MODEL=1` 是当初按 WorkBuddy 技能设计时的运维确认闸门，**独立服务下不消耗额度，但按约定保留**：除 `model-check` / `deps` / `install-launchd` 纯本地运维外，所有命令都要该环境变量，否则拒绝执行（见下方「免费模型约束」）。LaunchAgent plist 已内置。

## 安装第三方依赖（安装 / 重新拉取技能时必跑）

本技能除 Python 标准库外，**只依赖一个外部工具：CuaDriver**（trycua 出品的 GUI 应用，AX 驱动），它不会随技能仓库自动装好，且 mock 单测测不到、只在真实执行才暴露缺失。**安装（或 `git pull` 重新拉取）本技能后，agent 必须先执行以下检查再首次使用**：

```bash
./run.sh deps check     # 逐项检查，所有必需依赖 OK 才放行；缺失则退出码非 0，必须先补齐
```

依赖矩阵只有三项：**Python ≥ 3.11**（运行时）、**CuaDriver**（唯一必需外部工具：桌面签到 + 成长中心任意浏览器的 AX 操作，需在「系统设置 → 隐私与安全性 → 辅助功能」授权）、**浏览器（可选）**——Chrome/Edge/Safari 任一即可，用你惯用的浏览器从 WorkBuddy 点开「个人中心」，无需专门安装。CuaDriver 缺失时按 `deps check` 打印的指引人工安装并授权。

## 用法

手动执行：`BUDDY_FREE_MODEL=1 ./run.sh <command>`（`model-check` / `deps` / `install-launchd` / `inspect` 可不带）。

| 命令 | 作用 | 是否需要人工 |
|---|---|---|
| `deps check` | 逐项检查第三方依赖是否就绪（安装技能后必跑） | 否 |
| `deps install` | 打印 CuaDriver 的手动安装与授权指引（无 npm 依赖） | CuaDriver 需人工安装+授权 |
| `deps list` | 仅列出依赖与路径 | 否 |
| `inspect` | 只读诊断：窗口清单 + 客户端入口文案 + 成长中心探测（跨机器排查首选） | 否 |
| `status` | 只读查看三条时间线的下次执行时间 / 最近运行 | 否 |
| `checkin-now` | 手动跑**签到线**一轮（只签到） | 否（需 CuaDriver 授权） |
| `exchange-now` | 手动跑**兑换检查线**一轮 | 否 |
| `travel-now` | 手动跑**旅行线**一轮（领礼物→派遣→读真实倒计时） | 否 |
| `once` | 手动全量一轮（四条动作一起） | 否 |
| `daemon` | 统一常驻进程：三条独立时间线 | 否 |
| `install-launchd` | 写 LaunchAgent plist（RunAtLoad+KeepAlive） | 加载命令由你确认 |
| `model-check` | 查看免费模型策略与当前确认状态 | 否 |

## 统一常驻进程的三条时间线

进程启动时**三条线都立即执行一次**，之后各自从该时刻向后滚动：

| 线 | 周期 | 停止条件 |
|---|---|---|
| `checkin` | 每天 0 点恢复，每小时检查 | **确认**签到成功 → 禁用到当天 23:59 |
| `exchange` | 每天 0 点恢复，每小时检查 | 兑换成功 **或** 确认无可兑换 → 禁用到当天 23:59 |
| `travel` | 按页面真实倒计时滚动（唯一动态） | 不停，领奖→再派遣→读新倒计时 |

签到「确认」= DB 已记录今日成功 或 点击后按钮消失；仅凭按钮不存在推测已签不算确认，隔一小时再查（避免客户端页面未渲染就漏签）。兑换失败/窗口不可用同样隔一小时重试。三条线的下次时刻写入 SQLite，`status` 可查。

**领取成功判定**：出现「已领取」或页面回到可派遣（idle）/旅行中即成功；仍停在可领/弹窗/未知不算。**动作后清弹窗（三级）**：精确匹配的关闭按钮 → Escape（`CGEventPostToPid`，不移动光标）→ 复核快照确认；不碰浏览器标签页、不误点「确定派出」。领奖后弹窗未关会让派遣按钮 `AXEnabled=false`（2026-11-10 实测）。

**派遣次数当天用尽**：页面出现「明天再来 / 今日已派完 / 次数已用完」，或强关浮层后按钮仍 `AXEnabled=false`，判定 `dispatch_blocked=exhausted` → 旅行线禁用到当晚 23:00（`travel.exhausted_retry_at`，已过则次日 0 点），不再每小时重试；到点自动再检查。

## 成长中心与派遣时长

领礼物、派遣、签到兑换走 `growth` 通道：CuaDriver 读已登录成长中心页面的 AX 树（**不限定浏览器**——
从 WorkBuddy 点开进入哪个浏览器就在哪个浏览器操作，默认 `growth.browser_app="auto"` 按页面内容探测），
不注入 cookie、不需要 Bearer token。页面未登录时只提示用户先登录，不自动登录。状态由真实按钮判定
（派猫猫旅行 / 旅行倒计时 / 领取礼物）。

**派遣时长每次不同**：页面事前只提示「预计出行时间为1～4小时」。派遣后必须重新读
`旅行倒计时 HH:MM:SS`，用真实剩余秒数算 `due_at`，写入 SQLite；到期唤醒后领礼物、
立刻再派遣、再读新倒计时生成下一次 due。禁止使用固定间隔或写死 4 小时。

兑换保守：`兑换奖励` 只点弹窗里已解锁的档位（未解锁则关闭跳过），`抽奖(N)` 仅 N>0，
`开启盲盒 a/b` 仅能量满；补登卡和日期按钮永不自动消耗。

## 执行约束

- **不激活窗口、不移动物理鼠标、不接管用户日常浏览器**：所有点击优先用 `element_token`（无则 `element_index`）走 AX press，并校验返回 `path == "ax"`；任何会移动光标的像素路径一律拒绝。成长中心只操作用户自己打开的浏览器窗口。
- **签到按钮需要先展开面板**：客户端「Buddy加油站」面板收起时 AX 树里没有「立即领取」。必须先点开入口再重新快照定位；展开后仍无按钮、或页面出现「今日已领取」= 确认已签；连入口都找不到则报「状态不确定」、隔一小时再查，**绝不写 `last_checkin_day`**。
- **跨机器**：`AXPress -25202`（元素引用失效）已内置 3 次「重新快照定位」重试；入口文案用 `desktop.station_entry_labels` / `growth.auto_open_labels` 配置，差异先用 `./run.sh inspect` 只读查看。
- **页面免人工打开**：`growth.auto_open_page=true` 时，没有成长中心页面会先点客户端「做任务赢积分」入口，仍找不到则 `open -g` 后台打开目标 URL（不抢焦点，300 秒冷却）。未登录只提示、不自动登录。
- **状态判定只认 `AXButton`**：客户端里打开的 README/对话若含「派猫猫旅行」「领取 7 积分」等字样，不能被误判为成长中心页面；客户端窗口由 `growth.exclude_apps` 排除。
- **幂等**：所有领取经 SQLite 唯一键去重，重复运行不重复领。
- **时间口径**：一律北京时间；naive 时间按北京时间解释，不按 UTC。
- **凭据**：输出脱敏，不读取或导出客户端登录态与令牌；不自动登录。
- 活动入口与字段随版本变化，接口/文字定位失败时如实报告，不盲目重试。

详细架构、人工步骤与故障排查见 `README.md`。需求拆解与验收状态见 `prd.json`。

## 免费模型约束（历史约定，独立服务下不耗额度）

运行时不依赖 WorkBuddy agent / 模型，但 `BUDDY_FREE_MODEL=1` 闸门按约定保留：

1. 手动执行前以 `BUDDY_FREE_MODEL=1` 显式确认，否则命令拒绝执行（退出码 2）。
2. `install-launchd` 生成的 plist 已内置 `BUDDY_FREE_MODEL=1`，常驻进程无需额外操作。
3. 查看策略：`run.sh model-check`（纯本地运维，不需要该变量）。

## Git 管理

代码托管于 `https://github.com/timesky/t-buddy-checkin`（`main`）。本机通过 SSH key（`~/.ssh/id_rsa_tangming` / `lcc_op_rsa`，`timesky` 身份）读写。受 `.gitignore` 控制的本地产物（`config.toml`、`tools/node_modules/`、`*.png`、`*.json` 运行时产物、`*.sqlite3` 等）不入库。
