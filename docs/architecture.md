<style>
  :root { --wb-max-width: 980px; }
  body { max-width: var(--wb-max-width); margin: 0 auto; padding: 0 24px 48px; }
  pre, table { max-width: 100%; overflow-x: auto; }
  img { max-width: 100%; height: auto; }
</style>

# 架构与安全边界

[← 文档索引](README.md) · [端口](ports.md) · [签到判据](checkin-semantics.md)

## 链路

```
WorkBuddy 客户端 (Electron)
  │  WORKBUDDY_REMOTE_DEBUGGING_PORT=9222
  │  → 主进程 appendSwitch('remote-debugging-port')
  ▼
CDP 127.0.0.1:9222
  ├── [page]   客户端主 renderer      ← agent-browser 跨 webContents 点「成长计划」→「立即前往」
  └── [iframe] growth-center (OOPIF)  ← tools/cdp_client.mjs 直连执行 JS：读状态 / 点按钮
                                        ↓ Input.dispatchMouseEvent（trusted）
                                      补登日历 / 兑换 / 抽奖 / 派遣
```

`agent-browser` 负责跨 webContents 找入口（入口按钮不在主 renderer 的 DOM 里，只有它的
聚合快照能点到）；`cdp_client.mjs` 负责进 iframe 干细活。

## Electron 侧的实测约束（2026-11-12 验证，5.11.3）

| 约束 | 后果 |
|---|---|
| 客户端**不认** `--remote-debugging-port` 启动参数 | 只能设环境变量 `WORKBUDDY_REMOTE_DEBUGGING_PORT` |
| 环境变量只在启动时读一次，端口被占无回退 | 必须先探端口再启动（见 [ports.md](ports.md)） |
| 成长中心是独立 CDP target（`type=iframe`） | 可以直连 WebSocket 执行 `Runtime.evaluate` |
| `Target.createTarget` 被生产版禁用 | 不能 `tab new`，只能用既有页面 |
| 签到卡在 shadow DOM 内，且是远端下发 HTML | `app.asar` 里搜不到，light DOM 永远为空 |

## 模块职责

```
run.sh                    唯一入口（校验 Python，转调 python -m buddy_checkin）
src/buddy_checkin/
  cli.py                  命令分发、唯一端口注入点 inject_port()、版本闸门
  compat.py               版本红线（≥5.11）、官方下载直链、升级文案
  config.py               config.toml 解析与默认值
  ports.py                CDP 端口发现/归属判定/协商/持久化
  deps.py                 六项依赖体检
  launchd.py              两份 plist 渲染与 bootstrap/bootout
  progress.py             当日账本（五任务、退避表、翻页）
  scheduler.py            五个任务的编排与证据记录
  actions.py              任务级动作（领取/兑换/抽奖/派遣的原子操作）
  state.py                SQLite 幂等键（claim_once）
  timeutil.py             北京时间口径
  adapters/
    base.py               适配器接口
    fuel_card.py          「Buddy加油站」浮层签到 + 判据 judge_checked_today()
    growth_browser.py     成长中心 iframe：日历扫描、补签、兑换、抽奖、派遣
    desktop_agent.py      可选兜底（AX），不参与日常流程
tools/
  cdp_client.mjs          iframe 内 JS 执行 + CDP 会话
  cdp_input.mjs           真实鼠标事件（trusted click）
```

零第三方 Python 包（只用 `tomllib` / `sqlite3` / `zoneinfo` 等标准库）；npm 侧只有
`agent-browser`（必需）与 `agent-desktop`（可选）。

## 运行时数据

| 路径 | 内容 |
|---|---|
| `~/.local/share/buddy-web-checkin/progress.json` | 当日账本（五任务状态与证据） |
| `~/.local/share/buddy-web-checkin/state.sqlite3` | 幂等键，防重复领取 |
| `~/.local/share/buddy-web-checkin/cdp_port.json` | 端口缓存（最后可用的值） |
| `~/.local/share/buddy-web-checkin/daemon.log` | 常驻进程日志 |
| `~/.local/share/buddy-web-checkin/browser-profile/` | agent-browser 的临时 profile |
| `~/Library/LaunchAgents/com.local.*.plist` | 端口登录项 + daemon 登录项 |

## 安全边界：不做的事

- **不抢焦点**：所有操作走 CDP 协议（DOM 级 click），不激活窗口、不移动光标、不合成物理输入
- **不碰登录态**：不自动登录、不填账号密码、不绕验证码、不读取或导出任何 token
- **不做任务**：「去完成」一类任务按钮永不点击
- **不超权消费**：兑换只点弹窗里已解锁档位；`抽奖(N)` 仅 N>0；补登卡与日期按钮永不自动消耗
- **不猜**：状态只认结构（`.gs-cal-cell` 类名、`button` 的 `disabled`、「旅行倒计时」文本），
  不靠模糊文案
- **不降级**：CDP 不可达时明确报错并给修复命令，不偷偷换通道导致误报成功
- **可追溯**：每个 `done` 都带可复核证据字段（`checked_date` / `judge` / `gap` / `streak` 等）
- **会收尾**：任务结束关闭自己打开的成长中心弹窗；用户自己打开的页面保持原样

## 测试

```bash
python3 -m unittest discover -s tests -q     # 159 项
```

| 测试 | 覆盖 |
|---|---|
| `test_compat.py` | 版本解析（4 段构建号 / preview 后缀）、5.9.3 必须被拦、读不到版本不能误判 |
| `test_ports.py` | 端口归属判定、协商、`--port` 冲突拒绝、报告渲染 |
| `test_checkin_rollover.py` | 布尔为真但今天不在 `checkin_dates` 时不得记终态 |
| `test_makeup_lottery.py` | 补签三态、缺口交叉校验、抽奖到 0 |
| `test_fuel_card.py` / `test_exchange_guard.py` / `test_close_page.py` / `test_session_heal.py` / `test_deps.py` | 浮层、兑换守卫、收尾、会话自愈、依赖体检 |

单测全部 mock 掉 CDP，不依赖真机状态；真机验证用 `./run.sh inspect` 与 `once`。
