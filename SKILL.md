---
name: buddy-web-checkin
description: 独立运行 WorkBuddy 每日签到、签到礼物自动兑换、派猫猫旅行自动派遣，并在旅行精确到期时领取礼物后立刻再次派遣。仅签到依赖桌面客户端，其余走已登录浏览器里的成长中心页面；全程后台隔离，不干扰人工操作。触发词：帮我签到、领今日礼包、自动签到、签到礼物兑换、派猫猫旅行、自动派遣、领取旅行礼物、旅行到期领奖、签到守护进程。
---

# buddy-web-checkin

这是一个**独立 CLI 技能**，核心运行不依赖 WorkBuddy 的定时任务或技能机制：调度器是自带的标准库 `heapq`，状态在 SQLite。**所有自动化操作（桌面签到 + 成长中心领礼物/派遣/兑换）都走 CuaDriver 无障碍树（AX 通道，已授权）**——它是本技能唯一的外部工具与唯一实际可用的后端；不使用 headless 浏览器（agent-browser / agent-desktop / CDP / computer-use 均已移除）。

> 本技能**依赖 WorkBuddy**（签到在客户端内、成长中心是 WorkBuddy 个人中心），作为 WorkBuddy 技能 / 定时任务运行会消耗模型额度，**必须运行在「免费模型」下**：执行前先在模型选择器选免费模型，再以 `BUDDY_FREE_MODEL=1` 确认，否则命令拒绝执行（见下方「免费模型约束」）。

## 安装第三方依赖（安装 / 重新拉取技能时必跑）

本技能除 Python 标准库外，**只依赖一个外部工具：CuaDriver**（trycua 出品的 GUI 应用，AX 驱动），它不会随技能仓库自动装好，且 mock 单测测不到、只在真实执行才暴露缺失。**安装（或 `git pull` 重新拉取）本技能后，agent 必须先执行以下检查再首次使用**：

```bash
./run.sh deps check     # 逐项检查，所有必需依赖 OK 才放行；缺失则退出码非 0，必须先补齐
```

依赖矩阵只有三项：**Python ≥ 3.11**（运行时）、**CuaDriver**（唯一必需外部工具：桌面签到 + 成长中心任意浏览器的 AX 操作，需在「系统设置 → 隐私与安全性 → 辅助功能」授权）、**浏览器（可选）**——Chrome/Edge/Safari 任一即可，用你惯用的浏览器从 WorkBuddy 点开「个人中心」，无需专门安装。CuaDriver 缺失时按 `deps check` 打印的指引人工安装并授权。

## 用法

所有命令从项目根目录执行 `./run.sh <command>`：

| 命令 | 作用 | 是否需要人工 |
|---|---|---|
| `deps check` | 逐项检查第三方依赖是否就绪（安装技能后必跑） | 否 |
| `deps install` | 打印 CuaDriver 的手动安装与授权指引（无 npm 依赖） | CuaDriver 需人工安装+授权 |
| `deps list` | 仅列出依赖与路径 | 否 |
| `status` | 只读查看下次签到 / 旅行到期 / 最近运行 | 否 |
| `checkin-now` | 只做每日签到（桌面端） | 否（需 CuaDriver 辅助功能授权） |
| `once` | 立即跑一轮完整闭环 | 否 |
| `daemon` | 常驻精确调度：每日 9:30 签到+兑换检查；旅行到期自动领奖→再派遣 | 否 |
| `install-launchd` | 写 LaunchAgent plist（开机常驻，RunAtLoad+KeepAlive） | 加载命令由你确认执行 |
| `model-check` | 查看免费模型策略与当前确认状态 | 否 |

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

- **不激活窗口、不移动物理鼠标、不接管用户日常浏览器**：AX 点击用 `element_index`，走 AX press；成长中心只操作用户自己打开的浏览器窗口。
- **幂等**：所有领取经 SQLite 唯一键去重，重复运行不重复领。
- **时间口径**：一律北京时间；naive 时间按北京时间解释，不按 UTC。
- **凭据**：输出脱敏，不读取或导出客户端登录态与令牌；不自动登录。
- 活动入口与字段随版本变化，接口/文字定位失败时如实报告，不盲目重试。

详细架构、人工步骤与故障排查见 `README.md`。需求拆解与验收状态见 `prd.json`。

## 免费模型约束（重要）

本技能依赖 WorkBuddy，作为 WorkBuddy 技能 / 定时任务运行会消耗模型额度，**必须运行在「免费模型」下**：

1. 每次执行前（含定时器触发），先在 WorkBuddy 模型选择器选「免费模型」。
2. 命令行以环境变量 `BUDDY_FREE_MODEL=1` 显式确认已选免费模型，否则命令拒绝执行（退出码 2）。
3. `install-launchd` 生成的 plist 已内置 `BUDDY_FREE_MODEL=1`，定时任务无需额外操作。
4. 查看策略：`run.sh model-check`。

## Git 管理

代码托管于 `https://github.com/timesky/t-buddy-checkin`（`main`）。本机通过 SSH key（`~/.ssh/id_rsa_tangming` / `lcc_op_rsa`，`timesky` 身份）读写。受 `.gitignore` 控制的本地产物（`config.toml`、`tools/node_modules/`、`*.png`、`*.json` 运行时产物、`*.sqlite3` 等）不入库。
