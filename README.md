<style>  
  :root { --wb-max-width: 980px; }  
  body { max-width: var(--wb-max-width); margin: 0 auto; padding: 0 24px 48px; }  
  pre, table { max-width: 100%; overflow-x: auto; }  
  img { max-width: 100%; height: auto; }  
</style>

# buddy-web-checkin

独立的每日签到 / 礼物兑换 / 派遣自动化技能。**只有每日签到依赖 WorkBuddy 桌面客户端**，领礼物 / 派遣 / 兑换在成长中心页面完成。全部操作走 **CuaDriver** 的 AX 通道：不弹窗、不激活窗口、不抢鼠标键盘、不碰你正在用的浏览器。

零第三方 Python 包（仅标准库 `tomllib` / `sqlite3` / `zoneinfo`）。**唯一外部工具是 CuaDriver**（trycua 出品的 GUI 应用，已授权 `com.trycua.driver`）：签到操作 WorkBuddy 客户端窗口，成长中心操作你从 WorkBuddy 点开的浏览器窗口（任意浏览器，未登录只提示、绝不自动登录）。不读取、不解密、不导出任何客户端登录态或令牌。曾经的 `agent-browser` / `agent-desktop` / CDP / computer-use 通道均已移除（详见文末变更记录）。

## Git 管理

代码托管于 `https://github.com/timesky/t-buddy-checkin`（`main` 分支），本机通过 SSH key 读写（`~/.ssh/id_rsa_tangming` 与 `lcc_op_rsa`，均为 `timesky` 身份，已验证可推送）。

- 远程为空仓库，首次推送即创建 `main`：`git push -u origin main`。
- 受 `.gitignore` 控制的本地产物**不入库**：`config.toml`（含机器路径）、`tools/node_modules/`、`last-checkin-result.json`、`discovered.json`、`progress.txt`、`*.command`、`*.sqlite3`。
- 新克隆后：`cp config.example.toml config.toml` 按需改即可，无 npm 安装步骤。

## 运行时依赖与免费模型约束（重要）

**可作为独立服务执行**：运行时不依赖 WorkBuddy agent、不调用任何模型或在线 API。代码是纯 Python 3.11+ 标准库 + CuaDriver CLI，由 macOS LaunchAgent 直接拉起，不经 WorkBuddy 会话、不消耗模型额度。

但「被操作的对象」是 WorkBuddy：

- 签到要点 **WorkBuddy 桌面客户端**窗口里的签到按钮 → 客户端需保持运行；
- 成长中心是 **WorkBuddy 个人中心网页** → 浏览器需已登录且窗口打开（任意浏览器）；
- CuaDriver 需 macOS 辅助功能授权。

`BUDDY_FREE_MODEL=1` 是当初按「WorkBuddy 技能」设计时加的运维确认闸门，**独立服务下并不消耗额度，但按约定保留**：除 `model-check` / `deps` / `install-launchd` 等纯本地运维外，所有命令都要求该环境变量，否则拒绝执行（退出码 2）。LaunchAgent plist 已内置。

- 手动执行：`BUDDY_FREE_MODEL=1 ./run.sh <command>`
- 查看策略：`run.sh model-check`

说明：脚本无法读取当前选中的模型，故以运维侧显式确认作为放行闸门，避免误在付费模型下运行。

## 第三方依赖（安装技能时必跑 `deps check`）

本技能除 Python 标准库外，**只依赖一个外部工具：CuaDriver**。安装（或重新拉取）本技能后，agent 必须先跑：

```bash
./run.sh deps check   # 逐项检查，全部 OK 才放行；缺失则返回非 0 退出码并给安装指引
```

依赖矩阵（2026-11-09 按实际调用核对）：

| 依赖                         | 类型     | 必需        | 用途                                               | 安装方式                                       |
| -------------------------- | ------ | --------- | ------------------------------------------------ | ------------------------------------------ |
| Python ≥ 3.11（推荐 3.13）     | 运行时    | ✅         | 标准库 tomllib/sqlite3/zoneinfo                     | 系统自带 / python.org                          |
| CuaDriver                  | GUI 应用 | ✅         | 签到（WorkBuddy 客户端 AX）+ 成长中心（浏览器窗口 AX），**唯一自动化后端** | 安装 CuaDriver.app，并在 系统设置→隐私与安全性→辅助功能 授权其驱动 |
| 浏览器（Chrome/Edge/Safari 任一） | GUI 应用 | 否（已有其一即可） | 成长中心页面载体，用你惯用的浏览器从 WorkBuddy 点开即可                | 无需专门安装                                     |

## 签到通道（2026-11-09 真实跑通）

系统级 TCC 数据库给出决定性答案：`com.trycua.driver` 是 `auth=2`（已授权），而 `com.apple.Terminal` 和 `com.tencent.workbuddy.mac` 都是 `auth=0`。所以唯一可行后端是 **CuaDriver**。流程：

1. `cua-driver call list_windows` 找 WorkBuddy 可见主窗口（实测 `pid=452` / `window_id=66`）。
2. `get_window_state` 重新索引。**签到按钮藏在「Buddy加油站」面板里**：面板收起时 AX 树只有入口
   `AXButton "Buddy加油站"`，**没有**「立即领取」。必须先点开入口、再取一次新快照重新定位
   `AXButton "立即领取"`（实测展开后 `element_index=64`）。
3. `click` 优先用 `element_token`（来自最近一次快照，失效会显式报 stale），无 token 才回退
   `element_index`；并且**校验返回 `path == "ax"`，任何非 AX 路径一律拒绝**——像素路径会合成
   鼠标事件、把光标甩到窗口左上角。纯 AX press 不移动光标、不抢焦点、不激活窗口。
4. 再取一次快照复核：按钮与卡片消失即签到成功（1360 → 985 个元素）。

> **2026-11-10 真实踩坑**：旧逻辑在面板收起时找不到「立即领取」就判定「今天已签到」并写库
> `last_checkin_day`，造成**整天假阳性、根本没签到**，之后所有轮询都被幂等跳过。现在规则是：
> 面板未展开 → 先展开再判断；展开后仍无按钮 = 确认已签；连入口都找不到 = **状态不确定**
> （不写库、隔一小时再查）。只有 `confirmed=true` 才落库。

前置条件：CuaDriver daemon 必须在自身身份下运行：

```bash
/Applications/CuaDriver.app/Contents/MacOS/cua-driver permissions grant
/Applications/CuaDriver.app/Contents/MacOS/cua-driver permissions status --json   # accessibility: true
```

`permissions grant` 会通过 LaunchServices 以 `com.trycua.driver` 身份启动 daemon（`responsible_ppid=1`），之后 `checkin-now` / `once` / `daemon` 都复用它。

## 成长中心：领礼物 / 派遣 / 签到兑换（2026-11-09 真实跑通）

通道：用 CuaDriver 的 AX 能力读**已登录的成长中心页面**（`/profile/growth-center`）。**不限定浏览器**——你从 WorkBuddy 点开链接进入哪个浏览器，就在哪个浏览器操作（默认 `growth.browser_app = "auto"`，按页面内容而非固定 app 探测窗口）；如需限定可填浏览器 app 名。不用注入 cookie，也不碰需要 Bearer token 的签到接口。**页面未登录时只提示你先登录，绝不自动登录。**

| AX 状态          | 页面按钮             | 工具动作                          |
| -------------- | ---------------- | ----------------------------- |
| `idle`         | `派猫猫旅行`          | 点开场景弹窗 → `确定派出`               |
| `traveling`    | `旅行倒计时 00:59:56` | 解析真实剩余秒数 → 到期时间戳              |
| `claimable`    | `领取礼物`           | 打开归来弹窗 → `领取 7 积分` → 复核 `已领取` |
| `gift_dialog`  | `领取 N 积分`        | 直接领取并复核                       |
| `scene_dialog` | `确定派出`           | 确认派遣后立刻重读倒计时                  |

**派遣时长动态处理（关键）**：派遣前页面只写「预计出行时间为1～4小时」，实际每次不同。工具在**派遣之后**从 `旅行倒计时 HH:MM:SS` 读真实剩余秒数，`due_at = now + remaining + claim_offset_sec`（默认 3 秒，防服务端未结算），写入 SQLite。`daemon` 精确等到期 → 领礼物 → 再派遣 → 立刻读新的倒计时 → 生成下一次 due。每轮都按当次实际时长算，不套固定间隔；关机错过的到期在启动补偿时也会重新读页面现状。

**签到兑换（保守 + 幂等）**：`兑换奖励`（连登里程碑）会弹出「选择奖励」，只点真正已解锁的档位；当前是「还差 1 / 8 / 22 天」就自动关弹窗并跳过。`抽奖(N)` 仅 N>0、`开启盲盒 a/b` 仅能量满（a≥b）才点；**补登卡和日期按钮是有限资源，永不自动消耗**。每项按「北京时间日期 + 类型 + 文案」幂等去重。

实测 `./run.sh once`：签到已签 → 礼物无可领（旅行中）→ `milestone: no unlocked reward yet` → `traveling; due from real countdown (2912s)`，`travel_due_at = 2026-11-09 17:29:17 +0800`。

## 架构

```
每日签到 ──────────> CuaDriverSigner ──> cua-driver AX 通道
                     （WorkBuddy 客户端窗口，element_index 点击 + 复核）

礼物兑换 / 派遣 / 到期领奖 ──> GrowthCenter ──> cua-driver AX 通道
                              （任意浏览器里已登录的成长中心窗口）

精确到期 ──> 派遣后从「旅行倒计时 HH:MM:SS」读真实剩余秒数
         ──> 换算成北京时间绝对时间戳，写入 SQLite
         ──> heapq 调度器单次等到期 ──> 领奖 ──> 立刻再派遣 ──> 生成下一次到期
```

时间口径：**统一北京时间**（`Asia/Shanghai`）。带时区的 ISO 正常转换；naive 字符串按北京时间解释，绝不原样当 UTC。内部调度用 epoch 秒，不受本机时区影响。

## 快速开始

```bash
cd /path/to/buddy-web-checkin
cp config.example.toml config.toml      # 按需修改
chmod +x run.sh

./run.sh deps check    # 必跑：确认 CuaDriver 已安装并授权，否则后续命令会在真实环境才暴露缺失
./run.sh inspect      # 只读诊断：窗口清单 + 客户端入口文案 + 成长中心探测（跨机器排查首选）

# 手动命令（除 model-check / deps / install-launchd 外都要带 BUDDY_FREE_MODEL=1）
BUDDY_FREE_MODEL=1 ./run.sh status        # 只读：三条时间线的下次执行时间
BUDDY_FREE_MODEL=1 ./run.sh checkin-now   # 只跑签到线一轮
BUDDY_FREE_MODEL=1 ./run.sh exchange-now  # 只跑兑换检查线一轮
BUDDY_FREE_MODEL=1 ./run.sh travel-now    # 只跑旅行线一轮（领礼物→派遣→读倒计时）
BUDDY_FREE_MODEL=1 ./run.sh once          # 手动全量一轮
BUDDY_FREE_MODEL=1 ./run.sh daemon        # 统一常驻进程（前台，Ctrl-C 退出）

# 开机常驻（推荐）：写 plist 并加载，登录后自动运行 daemon
./run.sh install-launchd
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.buddy-web-checkin.plist
# 代码改动后重启常驻服务：
launchctl kickstart -k gui/$(id -u)/com.local.buddy-web-checkin
```

### 定时安排：统一常驻进程内的三条时间线

一个 `daemon` 进程、三条独立 Job 线，**进程启动时三条都立即执行一次**，之后各自从该时刻向后滚动：

| 线 | 周期 | 停止轮询条件 | 排程 |
|---|---|---|---|
| `checkin` 签到 | 每天 0 点恢复，每小时检查 | **确认**签到成功 → 禁用到当天 23:59 | 固定日周期 |
| `exchange` 兑换检查 | 每天 0 点恢复，每小时检查 | 兑换成功 **或** 确认无可兑换 → 禁用到当天 23:59 | 固定日周期 |
| `travel` 领礼物/派遣 | 按页面真实倒计时 | 不停，持续滚动 | **唯一动态** |

- 签到「确认」= DB 已记录今日成功 或 本轮点击后按钮消失；仅凭「按钮找不到」推测已签 **不算确认**，隔一小时再查，避免客户端页面未渲染就整天漏签。
- 兑换检查失败 / 窗口不可用则隔一小时重试，不会立刻禁用。
- 重启后旅行线先领礼物、再尝试派遣，然后按真实倒计时设置下次检查；派遣时长每次 1～4 小时不等，绝不套固定间隔。
- 三条线的下次时刻写入 SQLite，`status` 可直接查看。
- `run.sh` 自适应选择 Python ≥ 3.11（launchd 最小 PATH 下系统 `python3` 可能是 3.9，已验证修复），可用 `BUDDY_PYTHON` 指定解释器。

成长中心的登录**在你自己的浏览器里完成**：从 WorkBuddy 点开「个人中心」进入任意浏览器并登录，工具直接操作该窗口；检测到未登录时只会提示你登录，不做任何自动登录。

## 需要人工的两件事

| 事项                         | 为什么必须人工 | 具体做法                                                                                  |
| -------------------------- | ------- | ------------------------------------------------------------------------------------- |
| 在浏览器里登录 WorkBuddy 个人中心     | 账号归属    | 从 WorkBuddy 点开「个人中心」，在你惯用的浏览器里登录一次                                                    |
| 授予 macOS 辅助功能权限（CuaDriver） | 读取无障碍树  | CuaDriver（com.trycua.driver）实测已 `auth=2`，通常无需再授权；新机器上跑 `cua-driver permissions grant` |

## 精确到期与补偿

- 派遣后从页面倒计时读真实剩余秒数（派遣时长每次不同，绝不套固定间隔）。
- 到期后延后 `claim_offset_sec`（默认 3 秒）再领，避免服务端尚未结算。
- 领奖成功后立刻再次派遣，并重新读取到期时间，形成闭环。
- **领取成功判定**：出现「已领取」或页面回到可派遣（idle）/旅行中，即为成功；仍停在可领/弹窗/未知状态不算成功。
- **动作后清弹窗（三级）**：领奖、兑换、派遣完成后清理页面浮层，避免遮挡后续 AX 读取（实测
  12:51 领奖成功但弹窗没关 → 派遣按钮 `AXEnabled=false`）。① 点内容区精确匹配的
  「关闭 / 我知道了 / × / 收到」等按钮（天然排除浏览器「关闭标签页」）；② 找不到或仍在则
  发 **Escape**（`CGEventPostToPid`，不移动光标、不抢焦点）；③ 复核快照确认浮层真的消失。
- **派遣次数当天用尽**：页面出现「明天再来 / 今日已派完 / 次数已用完」等文案，或强关浮层后
  按钮仍 `AXEnabled=false`，判定 `dispatch_blocked=exhausted` → 旅行线**禁用到当晚 23:00**
  （`travel.exhausted_retry_at`，已过 23:00 则次日 0 点），不再每小时无效重试。
  派遣前也会先清浮层并重读状态。
- **重启不重置当天状态**：`travel_blocked` 连同 `travel_blocked_day` 落库。服务重启后
  `initial_jobs` 虽会立即跑三条线，但当天的 exhausted 标记会让它**直接跳到 23:00、
  不做实际检查**（避免重启误触发自动开页签）；跨天后自动清除。
- **签到假阳性防护（2026-11-12 实测）**：WorkBuddy 5.11.2 客户端右侧对话正文会含
  「明日再来」等历史消息。已签证据**只在「Buddy加油站」面板区内（入口索引 +
  `desktop.station_panel_window`，默认 16）且只认 AXButton**，绝不全文搜索正文。
  面板收起时先点开入口再判断；面板展开后确无「立即领取」按钮才确认已签。
- **签到满 24 小时**：落库 `last_checkin_at`（真实签到时刻），`daily_checkin.min_interval_sec`
  默认 86400；距上次真实签到不足 24h 即使跨了 0 点也不重复签，继续轮询。
- **web 动作前先刷新页面**（`growth.refresh_before_action=true`）：页面久置会保留旧状态
  （实测「今天已能派遣，页面仍显示昨天的『明天再来』」）。用纯 AX 点浏览器工具栏
  「刷新」按钮（不移动光标），等待 `refresh_wait_sec` 后重建快照再判断；同轮只刷一次。
- **兑换确定性结论**：弹窗里「本月已兑换 / 已兑完 / 已达上限」等判为**无法兑换**，
  返回 ok=True 让兑换线禁用到 23:59，而不是 ok=False 每小时无效重试。
- **派遣场景弹窗**：点「派猫猫旅行」后会进入场景选择弹窗，必须**重读快照**再点
  「确定派出」（不能用弹窗出现前的旧索引）；点击后异步更新，读不到 traveling 时
  等 1.2s 复核一次，避免把成功的派遣误判失败。
- **不重复开页签**：自动打开前先按窗口标题判断「是否已有 WorkBuddy 个人中心页签」，
  有就只读等待、不再新开；自动打开冷却 `last_auto_open_at` 写 kv，跨进程/重启生效。
  CuaDriver daemon 不可用时（`driver_unavailable`）一律不自动打开。
- **重启即补**：进程启动三条线都立即跑一轮，关机/睡眠期间错过的旅行到期会在启动时立刻领奖并再派遣；签到与兑换未确认的也会在启动后立即检查。
- 签到/兑换在每天 0 点恢复轮询；当天得到确定结论后禁用到 23:59，失败则隔一小时滚动重试。

## 幂等与安全边界

- 每次领取经 SQLite `(北京时间日期, 类型, 项目)` 唯一键去重，重启、重跑、定时重叠都不会重复领。
- 所有命令输出经脱敏，含 token / cookie / authorization 的行替换为 `<redacted>`；凭据不落盘、不进日志。
- 礼物兑换有 `allow_keywords` / `deny_keywords` 白黑名单，默认屏蔽「去完成 / 充值 / 购买 / 开通 / 升级」等消费类按钮。
- AX 点击不移动光标、不激活窗口；浏览器窗口只读已登录页面，不注入 cookie、不接管会话。
- 仅供本人账号、按页面当期规则使用；活动入口、额度、字段会随版本变化，以实际页面为准。

## 许可

本项目代码零第三方依赖。调用的唯一外部工具 CuaDriver 为独立 GUI 应用（其自身许可见 trycua 官方）。依赖树中不含 BUSL / FSL / PolyForm-NC 等限制性许可。

## 另一台机器 / 全新安装

```bash
git clone git@github.com:timesky/t-buddy-checkin.git
cd t-buddy-checkin && cp config.example.toml config.toml && chmod +x run.sh
./run.sh deps check        # CuaDriver 必须在「辅助功能」里授权 com.trycua.driver
./run.sh inspect           # 只读：看客户端入口文案、窗口清单、成长中心探测结果
BUDDY_FREE_MODEL=1 ./run.sh checkin-now    # 逐条验证，先看单条线结果
BUDDY_FREE_MODEL=1 ./run.sh exchange-now
BUDDY_FREE_MODEL=1 ./run.sh travel-now
./run.sh install-launchd   # 然后 launchctl bootstrap，或下次登录自动加载
```

**页面无需人工先点开**：`growth.auto_open_page=true` 时，浏览器里没有成长中心页面会先点客户端
「做任务赢积分」入口；点击后仍检测不到，就用 `open -g` 在后台打开
`https://www.workbuddy.cn/profile/growth-center`（不激活浏览器、不抢焦点）。
带 300 秒冷却，避免每次轮询重复开标签页。未登录仍只提示，绝不自动登录。

跨机器常见差异（2026-11-10 实测）：

- `AXUIElementPerformAction(AXPress) returned -25202` = 元素引用失效，已内置 3 次
  「重新取快照 → 重新定位 → 再点」的重试（`desktop.click_max_attempts`）。
- 客户端版本不同、入口文案不同 → `inspect` 看实际按钮，改
  `desktop.station_entry_labels` / `growth.auto_open_labels` / `dom_fallback.checkin`。
- 「成长计划」按钮实测打开的是 CodeBuddy 积分页，已从默认 `auto_open_labels` 移除。
- 客户端窗口里打开含「派猫猫旅行 / 领取 7 积分」的文档时曾被误判为成长中心：
  现在状态判定**只认 `AXButton`** 标签，并排除客户端窗口（`growth.exclude_apps`）。

## 故障排查

| 现象                                   | 检查                                                       |
| ------------------------------------ | -------------------------------------------------------- |
| `deps check` 报 CuaDriver MISSING     | 安装 CuaDriver.app 并 `cua-driver permissions grant` 授权辅助功能 |
| 签到报 WorkBuddy main window not found  | 确认 WorkBuddy 客户端已打开且主窗口可见                                |
| 成长中心报 login_required                 | 在浏览器里登录 WorkBuddy 个人中心后重跑（工具不自动登录）                       |
| 成长中心报 growth-center window not found | 现在会**自动**点「做任务赢积分」入口、并后台 `open -g` 打开成长中心 URL；仍失败看 `./run.sh inspect` |
| 签到报 `AXPress ... -25202` | 元素引用失效，已内置 3 次「重新快照定位」重试；仍失败看 `inspect` 的客户端按钮文案 |
| 领奖后派遣报 `AXEnabled=false` | 弹窗遮挡：已加三级清理（扩展关闭按钮文案 + Escape 兜底 + 复核），派遣前也会先清弹窗 |
| 页面显示「明天再来 / 次数已用完」 | 当天派遣次数用尽，旅行线自动禁用到 23:00（`travel.exhausted_retry_at`），`status` 可看下次时间 |
| 反复新开 WorkBuddy 页签 | 已修：先判断已有个人中心页签就不新开；自动打开冷却写 kv，重启不重置 |
| `list_windows` 失败 / `Cua Driver daemon is not running` | **CuaDriver daemon 挂了**（会连锁导致探测失败、反复开页签、派遣重试）。用 `cua-driver permissions grant` 重新拉起（以 com.trycua.driver 身份，否则辅助功能授权不生效）；daemon 不可用时本技能不会自动开页签 |
| 今天明明没签到却显示已签 / 整晚不补签 | 对话正文的「明日再来」被误当证据（5.11.2 实测）。已修：已签证据只在加油站面板区内认按钮；确认异常时清 `last_checkin_day`/`last_checkin_at` 后重跑 `checkin-now` |
| 页面状态不变（如今天可派遣却显示「明天再来」） | 页面久置未刷新。已加 web 动作前自动刷新（`growth.refresh_before_action`），也可手动 `travel-now` 复验 |
| 派遣停在 `state=scene_dialog` | 点「派猫猫旅行」后才出场景弹窗，已修为重建快照再点「确定派出」+ 异步复核 |
| 客户端文档/对话被误判成成长中心 | 已修：状态判定只认 `AXButton`，并排除客户端窗口（`growth.exclude_apps`） |
| 另一台机器入口文案不同 | `./run.sh inspect` 看实际按钮，改 `desktop.station_entry_labels` / `growth.auto_open_labels` |
| 领取没到账                                | `./run.sh status` 看运行日志；签到/兑换失败会隔 1 小时自动重试      |

## 变更记录：移除未使用的工具（2026-11-09）

早期版本曾内置多条候选通道，真实执行后确认只有 CuaDriver 可用，其余全部移除以免混淆：

- **`agent-browser`**（headless 浏览器 + `setup` 扫码 / `setup-from-edge` 复用 Edge 登录态 / `probe` 探测）：已被「从 WorkBuddy 点开浏览器 + 未登录提示」取代，代码与 `web` 配置段一并删除。
- **`agent-desktop`**（桌面端回退引擎）：TCC 授权对象不是宿主进程，实测 `PERM_DENIED`，从未可用，代码与 npm 依赖一并删除。
- **CDP / computer-use**（桌面端备选）：前者需带调试端口重启客户端、后者会移动物理鼠标，均删除。
- `desktop.engine` 配置项随之移除——桌面端后端只有 CuaDriver，无需选择。
