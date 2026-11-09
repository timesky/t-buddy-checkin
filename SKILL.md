---
name: buddy-web-checkin
description: 独立运行 WorkBuddy 每日签到、签到礼物自动兑换、派猫猫旅行自动派遣，并在旅行精确到期时领取礼物后立刻再次派遣。仅签到依赖桌面客户端，其余走网页端；全程后台隔离，不干扰人工操作。触发词：帮我签到、领今日礼包、自动签到、签到礼物兑换、派猫猫旅行、自动派遣、领取旅行礼物、旅行到期领奖、签到守护进程。
---

# buddy-web-checkin

这是一个**独立 CLI 技能**，核心运行不依赖 WorkBuddy 的定时任务或技能机制：调度器是自带的标准库 `heapq`，状态在 SQLite，网页端用 `agent-browser` headless 独立会话，**桌面端签到用 CuaDriver 无障碍树（AX 通道，已授权；agent-desktop / CDP 为引擎回退）**。

> 本技能**依赖 WorkBuddy**（签到在客户端内、成长中心是 WorkBuddy 个人中心），作为 WorkBuddy 技能 / 定时任务运行会消耗模型额度，**必须运行在「免费模型」下**：执行前先在模型选择器选免费模型，再以 `BUDDY_FREE_MODEL=1` 确认，否则命令拒绝执行（见下方「免费模型约束」）。

## 安装第三方依赖（安装 / 重新拉取技能时必跑）

本技能除 Python 标准库外，依赖若干**外部 CLI / GUI 应用**（agent-browser、CuaDriver、Microsoft Edge、可选 agent-desktop），它们**不会**随技能仓库自动装好，且 mock 单测测不到、只在真实执行才暴露缺失。**安装（或 `git pull` 重新拉取）本技能后，agent 必须先执行以下两步再首次使用**：

```bash
./run.sh deps install   # 装经 npm 管理的依赖；GUI 依赖（CuaDriver / Edge）给出手动安装与授权指引
./run.sh deps check     # 逐项检查，所有必需依赖 OK 才放行；缺失则退出码非 0，必须先补齐
```

`deps check` 会据 `config.toml` 各通道开关动态判定「必需 / 可选」：growth 通道开启或桌面端用 cua-driver 引擎时，**CuaDriver + Microsoft Edge 为必需**；网页端 headless 驱动 **agent-browser 始终必需**；agent-desktop 仅在使用 agent-desktop 引擎时必需（默认 cua-driver 下为可选回退）。GUI 依赖缺失时，`deps install` 只打印指引，需人工安装并在「系统设置 → 隐私与安全性 → 辅助功能」为 CuaDriver 授权。

## 用法

所有命令从项目根目录执行 `./run.sh <command>`：

| 命令 | 作用 | 是否需要人工 |
|---|---|---|
| `deps install` | 安装经 npm 管理的依赖；GUI 依赖给出手动安装指引 | GUI 依赖需人工安装+授权 |
| `deps check` | 逐项检查第三方依赖是否就绪（安装技能后必跑） | 否 |
| `probe` | 只读探测网页端入口与桌面端可用后端，写 `discovered.json` | 否 |
| `setup` | headless 打开登录页，二维码截图存 `login-qr.png` | 手机扫码一次 |
| `setup-from-edge` | 只读复制 Edge 已登录 profile 到独立副本，用 Edge 可执行文件验证 | 否（但需在非沙箱终端运行） |
| `status` | 只读查看下次签到 / 旅行到期 / 最近运行 | 否 |
| `checkin-now` | 只做每日签到（桌面端），不碰网页端 | 否（需辅助功能授权） |
| `once` | 立即跑一轮完整闭环 | 否 |
| `daemon` | 常驻精确调度，Ctrl-C 退出 | 否 |
| `install-launchd` | 写 LaunchAgent plist（不自动加载） | 加载动作由你确认 |
| `model-check` | 查看免费模型策略与当前确认状态 | 否 |

## 成长中心与派遣时长

领礼物、派遣、签到兑换走 `growth` 通道：CuaDriver 读已登录 Edge「个人中心」的 AX 树，
不注入 cookie、不需要 Bearer token。状态由真实按钮判定（派猫猫旅行 / 旅行倒计时 / 领取礼物）。

**派遣时长每次不同**：页面事前只提示「预计出行时间为1～4小时」。派遣后必须重新读
`旅行倒计时 HH:MM:SS`，用真实剩余秒数算 `due_at`，写入 SQLite；到期唤醒后领礼物、
立刻再派遣、再读新倒计时生成下一次 due。禁止使用固定间隔或写死 4 小时。

兑换保守：`兑换奖励` 只点弹窗里已解锁的档位（未解锁则关闭跳过），`抽奖(N)` 仅 N>0，
`开启盲盒 a/b` 仅能量满；补登卡和日期按钮永不自动消耗。

## 执行约束

- **不激活窗口、不移动物理鼠标、不接管用户日常浏览器**：网页端默认 headless + 独立 `--session` / `--profile`；桌面端 `allow_focus=false`。
- **先探测再动作**：首次使用必须先 `probe`，确认签到按钮在 AX 树里可见，再启用 `daemon`。
- **幂等**：所有领取经 SQLite 唯一键去重，重复运行不重复领。
- **时间口径**：一律北京时间；naive 时间按北京时间解释，不按 UTC。
- **凭据**：输出脱敏，不读取或导出客户端登录态与令牌。
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
