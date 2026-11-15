<style>
  :root { --wb-max-width: 980px; }
  body { max-width: var(--wb-max-width); margin: 0 auto; padding: 0 24px 48px; }
  pre, table { max-width: 100%; overflow-x: auto; }
  img { max-width: 100%; height: auto; }
</style>

# buddy-web-checkin

**WorkBuddy 每日活跃自动化**：签到、兑换、抽奖、派遣，五个任务一个账本，全程后台静默。

[![WorkBuddy](https://img.shields.io/badge/WorkBuddy-%E2%89%A5%205.11.0-6f6fe5)](docs/compatibility.md)
[![macOS](https://img.shields.io/badge/platform-macOS%2012%2B-lightgrey)](docs/compatibility.md)
[![Python](https://img.shields.io/badge/python-%E2%89%A5%203.11-3776ab)](docs/compatibility.md)
[![Node](https://img.shields.io/badge/node-%E2%89%A5%2022-339933)](docs/compatibility.md)
[![tests](https://img.shields.io/badge/tests-159%20passing-brightgreen)](docs/architecture.md#测试)

[English](README.en.md) · [文档索引](docs/README.md) · [SKILL.md](SKILL.md)

---

全部操作走 **CDP**：直连 WorkBuddy 客户端内嵌的成长中心页面做 DOM 级操作 ——
**不激活窗口、不移动光标、不抢鼠标键盘**，你照常用电脑。

零第三方 Python 包（只用标准库 `tomllib` / `sqlite3` / `zoneinfo`）；npm 侧只有
`agent-browser`（必需）与 `agent-desktop`（可选）。

## 一眼看懂

- **五任务 + 当日账本**：签到 → 兑换 → 补签 → 抽奖 → 派遣，每个任务给出 `done` /
  `impossible` / `pending` 三态之一，**只有全部终态才静默**，否则按 60→120→300→600s 退避重试。
- **判据可复核**：不靠界面文案，`done` 一律以服务端状态/页面结构为准（`checkin_dates` 含今天、
  奖励项变「已兑换」、抽奖券为 0、派遣次数用尽）。
- **不写死时间**：派遣从页面读真实「旅行倒计时」，到点先领奖励再派下一次。
- **重启会自纠**：启动时用真实状态复核账本，写错的 `done` 会被改回 `pending` 立即补做。
- **端口自适应**：跟随客户端自报的 `DevToolsActivePort`，被占时 `ports negotiate` 换端口，不写死 9222。

## 版本要求（不满足就先升级）

| 客户端版本 | 结论 |
|---|---|
| **≥ 5.11.0**（实测基线 5.11.3） | ✅ 支持 |
| 5.10.x | ❌ 未公开、未实测 |
| **≤ 5.9.x** | ❌ 不支持：成长中心还是**网页版活动中心**，客户端里没有内嵌成长中心和「Buddy加油站」浮层 |

> 5.11 目前是**灰度**版本：官网下载页最新只到 5.9.3，照着官网装会直接踩坑。
> 两条可用路径（客户端内「检查更新」/ 官方 CDN 直链）见
> **[兼容性文档](docs/compatibility.md)**。

**其余硬依赖**：macOS 12+、Python ≥ 3.11、Node **≥ 22**、agent-browser、
客户端**已登录且主窗口不关**、界面为**简体中文**（50+ 处按中文文案定位）。
完整清单与「不满足会怎样」见 [docs/compatibility.md §3](docs/compatibility.md#3-其余硬依赖)。

## 快速开始

```bash
git clone git@github.com:timesky/t-buddy-checkin.git
cd buddy-web-checkin
cd tools && npm install && cd ..          # agent-browser（必需）
cp config.example.toml config.toml        # 默认值可直接跑

./run.sh install-launchd                  # 写 daemon 登录项 + 端口持久化
./run.sh ports status                     # 没监听就：./scripts/launch-workbuddy-debug.sh

./run.sh deps check                       # 六项全 OK 才算装好
./run.sh inspect                          # 应看到 [iframe] growth-center + 签到状态
BUDDY_FREE_MODEL=1 ./run.sh once          # 跑一轮真实流程
```

每一步的期望输出、开机自启、卸载方式见 **[安装文档](docs/install.md)**；
端口为什么需要单独处理见 **[端口文档](docs/ports.md)**。

## 常用命令

```bash
BUDDY_FREE_MODEL=1 ./run.sh <command>
```

| 命令 | 作用 | 命令 | 作用 |
|---|---|---|---|
| `deps check` | 依赖体检（首项是版本红线） | `inspect` | 只读诊断，跨机器排查首选 |
| `status` | 当日账本 + 退避时间 | `once` | 按账本跑完五个任务一遍 |
| `checkin-now` | 只签到 | `daemon` | 常驻，全终态静默到 23:59 |
| `exchange-now` | 只兑换 | `install-launchd` | 装/重装开机自启 |
| `makeup-now` | 只补签 | `ports status` | 端口全貌与归属判定 |
| `lottery-now` | 只抽奖 | `ports negotiate` | 协商空闲端口（`--apply` 落盘） |
| `travel-now` | 只派遣 | `model-check` | 免费模型策略 |

除 `deps` / `inspect` / `install-launchd` / `model-check` / `ports` 外都要带
`BUDDY_FREE_MODEL=1`（免费模型闸门，plist 内已内置）。完整命令表、账本规则与退避策略见
**[用法文档](docs/usage.md)**。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/compatibility.md](docs/compatibility.md) | 版本红线、5.9.x 为什么不行、官方升级/下载路径、硬依赖清单 |
| [docs/install.md](docs/install.md) | 新机器安装 5 步 + 验证输出 + 开机自启 + 卸载 |
| [docs/usage.md](docs/usage.md) | 命令表、五个任务、当日账本、退避与静默 |
| [docs/ports.md](docs/ports.md) | CDP 端口自适应四级来源、归属判定、换端口流程 |
| [docs/checkin-semantics.md](docs/checkin-semantics.md) | 签到在哪、凭什么算签上、漏签怎么补（含图例色值） |
| [docs/architecture.md](docs/architecture.md) | 链路图、Electron 实测约束、模块职责、测试 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | 按现象查原因、跨机器排查、诊断字段含义 |
| [docs/changelog.md](docs/changelog.md) | 行为变化与踩过的坑 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 开发环境、测试约定、提交规范 |

## 安全边界

- **不碰登录态**：不自动登录、不填账号密码、不绕验证码、不读取或导出任何 token。
- **不超权消费**：兑换只点已解锁档位，`抽奖(N)` 仅 N>0，补登卡与日期按钮永不自动消耗。
- **不做任务**：「去完成」一类按钮永不点击。
- **不静默降级**：CDP 不可达时明确报错并给修复命令，不会换个通道假装成功。
- **会收尾**：只关自己打开的成长中心弹窗，用户自己打开的页面不动。

> **免责声明**：本项目通过客户端调试端口操作你自己的 WorkBuddy 客户端，属于本机自动化，
> 不破解、不代理、不改服务端数据。使用前请确认符合 WorkBuddy 用户协议与活动规则；
> 因自动化产生的账号风险与积分变动由使用者自行承担。

## 开发

```bash
python3 -m unittest discover -s tests -q     # 159 项，全部 mock 掉 CDP
BUDDY_FREE_MODEL=1 ./run.sh inspect          # 真机只读诊断
```

代码结构、模块职责与测试覆盖见 [docs/architecture.md](docs/architecture.md)；
提交前请读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## License

仓库暂未附 License 文件。在补上之前，默认保留所有权利；如需在别的项目里复用，
请先开 Issue 说明用途。
