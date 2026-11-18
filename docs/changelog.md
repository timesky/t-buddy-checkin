<style>
  :root { --wb-max-width: 980px; }
  body { max-width: var(--wb-max-width); margin: 0 auto; padding: 0 24px 48px; }
  pre, table { max-width: 100%; overflow-x: auto; }
  img { max-width: 100%; height: auto; }
</style>

# 变更记录

[← 文档索引](README.md) · [主 README](../README.md)

只记「行为/判据发生变化」和「踩过的坑」，不记纯重构。

## 2026-11-18 · 端口自愈 + 跨天强制翻页（另一台机器 11-18 全天没跑）

- **事故**：另一台机器 11-17 10:37 起 `checkin` 每 10 分钟报
  `CDP 9222 unreachable`，持续到 11-18 全天；浮层显示可签到但工具连不上，
  11-18 的派遣/领取/补签/兑换/抽奖全部没有执行。
- **原因一（端口缓存）**：`fuel_card.py` 构造时取 `config_port` 且永不重探；
  daemon 长驻期间客户端重启换到 9223 后，签到任务一直打旧端口 9222。
  `growth_browser` 每轮 `ensure_page()` 都 `ports.resolve()` 自愈，所以
  travel/exchange 昨天还能跑——同机同 daemon 两条路径行为不一致。
- **修复一**：`fuel_card.page_ws()` 增加 `_heal()`：CDP 不可达时按真实状态
  重探端口（env / active_file / 区间探测）并清 renderer 缓存后重试一次，
  与 `growth_browser._heal()` 行为对齐。
- **原因二（跨天不翻页）**：账本翻页只走「全部终态 → 静默 → 午夜醒来
  reset」路径；只要有任务整夜 pending（heap 永不为空），该路径永远走不到，
  账本日期停在昨天，新的一天其余四个任务挂着旧 done 全天无人调度。
- **修复二**：调度主循环在弹出任务时检查 `day_key(now) != progress.day`，
  跨天即强制 `reset_all` + 全量 `verify_pass`，不再依赖静默路径。
  `reset_all` 显式传调度器时钟的日期，避免依赖真实系统时间。
- **顺带**：`test_unmounted_slot_is_reopened_before_giving_up` 的假数据
  硬编码 11-15/11-16 日期，两天后自然失效——改为按真实今天/昨天构造。
- 单测 170 项全绿（新增跨天翻页守卫 1 项）。

## 2026-11-16 · 修复多窗口下签到整天漏签

- **事故**：11-16 00:00 账本正常翻页，但 `checkin` 从 00:00 到 10:58 每 10 分钟
  重试一次、共 69 次全部 `checkinData not found (slot not mounted)`，整天没签到。
  daemon 与 LaunchAgent 均正常，四项其它任务都 `done`。
- **原因一**：客户端同时存在 4 个 `app.asar` page，只有挂浮层的那个 shadow DOM 里有
  `#go`；`page_ws()` 取 `/json/list` 第一个 URL 匹配项，窗口顺序一变就选错页。
- **原因二**：`claim()` 先读状态、读不到直接返回失败，`ensure_visible()` 排在后面，
  浮层未挂载时形成「读不到 → 不唤起 → 继续读不到」的循环，重试不会自愈。
- **修复**：`page_ws()` 逐个探测选含浮层的 renderer 并缓存；CDP 失败/未挂载时清缓存
  重扫；回退页优先主窗口（跳过表格窗口）；`claim()` 读不到时先重新选页 +
  `ensure_visible()` 唤起浮层再读一次，仍失败才记 `pending`；失败证据新增 `reopen`
  字段区分 `reopened` / `reopen_failed` / `not_needed`。
- 单测 167 项全绿（新增 8 项覆盖选页、缓存失效、回退优先级、恢复顺序）。
- 当天 11:03 已用 `checkin-now` 补签：`credit=100`、`streak=38`、判据 `date_in_dates`。

## 2026-11-16 · 成长中心入口兜底（另一台机器礼物/派遣整天没做）

- **现象**：雷电网桥另一台机器（10.53.0.1）上，`exchange` / `makeup` / `lottery` /
  `travel` 四项全天 `pending`，`travel` 重试 47 次，全是
  `no growth entry found in client`；礼物没领、新派遣没发生。签到走浮层所以仍 `done`。
- **原因**：该机器客户端侧栏里没有 `["成长计划","做任务赢积分","任务中心"]` 任何一个
  按钮，但左下角浮层运营卡直接挂着一个 `#go` 且
  `data-track-props={"type":"growth"}` 的「立即前往」。旧代码只有「先点侧栏入口 →
  再点立即前往」一条路径，第一步失败就永远走不到第二步。
- **修复**：`ensure_page()` 在所有常规入口之后追加「立即前往」兜底；
  `_open_via_labels()` 认出该按钮本身就是最后一步，不再二次点击；点完仍必须验证
  growth-center iframe 出现才算成功，避免误点其它运营卡。
- 单测 169 项全绿（新增 2 项：兜底能打开、兜底排在常规入口之后）。
- 排查顺序与现象说明补进 [troubleshooting.md](troubleshooting.md)。

## 2026-11-15 · 兼容性前置（版本成一等公民）

- 新增 [`compat.py`](../src/buddy_checkin/compat.py)：集中放版本红线（≥ 5.11.0，实测基线
  5.11.3）、官方下载直链与升级指引，措辞只有一份。
- `deps check` **首项**校验客户端版本；`checkin-now` / `once` / `daemon` 在版本过低时
  **动手前退出**（退出码 `3`），不空跑一天。
- `node` 门槛从「装了就行」收紧到 **≥ 22**（`cdp_client.mjs` 用全局 `WebSocket`，
  `agent-browser` 的 `engines.node` 也是 `>= 22`）。
- `ports status` / `inspect` 打印客户端版本、安装路径与判定；客户端路径不再写死
  `/Applications`（`WORKBUDDY_APP` 可覆盖）。
- 文档补齐「5.9.x 是网页版活动中心、不支持」的原因与 5.11 灰度期的下载方式；
  文档从单文件 README 拆成 `docs/`，并补英文版 README。

## 2026-11-15 · 兑换先于补签；签到判据以服务端日期为准

- 任务顺序调整为 **checkin → exchange → makeup → lottery → travel**：补登卡来自兑换奖励，
  先拿卡才能当轮补签。
- 签到判据从裸布尔 `today_checked_in` 改为 `today in checkin_dates`；布尔仅作兜底。
  检测到「布尔为真但今天不在列表」→ `_wait_rollover` 等翻日（30s × 6，最多 180s），
  **绝不记终态**。
- 补签新增**渲染完整性**与**缺口交叉校验**两道闸（见 [签到判据](checkin-semantics.md)），
  两者不满足只记 `pending`，不下终态。

## 2026-11-14 · 五任务 + 当日账本

- 整体改为「五任务 + 当日账本」模型，账本 `progress.json` 三态
  `pending / done / impossible`，退避 `60 → 120 → 300 → 600s`，全终态静默到 23:59。
- 签到判据换成服务端 `checkinData`（`onRefreshCheckin` 现场刷新），不再用「卡片在不在」推测。
- 新增漏签自动补签（补签格只认 CDP 真实鼠标事件，JS `click()` 无效）、自动抽奖
  （抽奖是子页不是弹层）、兑换后必须复核奖励项变化。
- **重启不受静默控制**：启动即用真实状态复核账本，写错的 `done` 会被改回 `pending` 并补做。

## 2026-11-14 · 更正签到通道

- 签到一直在主窗口左下角「Buddy加油站」浮层（shadow DOM 内的远端 slot html），
  新增 [`adapters/fuel_card.py`](../src/buddy_checkin/adapters/fuel_card.py)；
  `GrowthBrowser.checkin()` 降级为「活跃地图只读参考」。
- 修正 11-12 的错误结论（当时因 `app.asar` 搜不到该文案就判定「按钮已取消」，
  导致签到线连着两天一次都没真正执行却报成功）。

## 2026-11-12 · 改用 CDP 通道

- 改用 CDP（`agent-browser` + 客户端调试端口）。删除 `adapters/desktop.py`（CuaDriverSigner）
  与 `adapters/growth.py`（GrowthCenter），CuaDriver 不再是依赖。
- 关键实测：客户端不认 `--remote-debugging-port` 参数只认环境变量；成长中心是
  `type=iframe` 的独立 target；`Target.createTarget` 被生产版禁用。

## 更早

- **2026-11-10**：引入 `agent-desktop` 第二引擎（严格 headless，不抢焦点）。
- **2026-11-09**：首版，CuaDriver AX 通道。
