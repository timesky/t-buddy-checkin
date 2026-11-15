<style>  
  :root { --wb-max-width: 980px; }  
  body { max-width: var(--wb-max-width); margin: 0 auto; padding: 0 24px 48px; }  
  pre, table { max-width: 100%; overflow-x: auto; }  
  img { max-width: 100%; height: auto; }  
</style>

# buddy-web-checkin

WorkBuddy 每日活跃（签到）/ 领礼物 / 派遣 / 兑换自动化。全部走 **CDP**：直连 WorkBuddy 客户端内嵌的成长中心页面做 DOM 级操作，不激活窗口、不移动光标、不抢鼠标键盘。

零第三方 Python 包（仅标准库 `tomllib` / `sqlite3` / `zoneinfo`）。运行时依赖：`node` + `agent-browser` + 客户端调试端口。旧 CuaDriver / AX 通道已整体移除。

## 兼容性：版本不对，后面全白做

### 支持的 WorkBuddy 版本

| 客户端版本 | 结论 | 说明 |
|---|---|---|
| **≥ 5.11.0** | ✅ 支持 | 客户端内置成长中心 + 左下角「Buddy加油站」浮层，五个任务齐全。**实测基线 5.11.3**；比基线更新的版本仍放行，只提示先跑 `inspect` 复核 |
| 5.10.x | ❌ 不支持 | 官方更新日志里没有公开的 5.10，也未实测，按不支持处理 |
| **≤ 5.9.x** | ❌ 不支持 | 成长中心还是**网页版活动中心**：客户端里没有内嵌成长中心（CDP 里看不到 `type=iframe` 的 growth-center），也没有「Buddy加油站」浮层。本技能两条通道都不存在，**不做降级兼容** |

> **预览 / 灰度版**（版本号带 `-preview`、`-beta`、`-rc` 之类后缀）：只要 `major.minor`
> ≥ 5.11 就放行，`compat` 只把通道标成 `preview` 写进日志，不做限制。
> 下载仍走下面两条官方地址（5.11.3 本身就是灰度构建）。
> **比实测基线更新**（如 5.12+）：同样放行，但会提示先跑 `inspect` 复核 DOM。

版本号怎么查（三选一，都不会改东西）：

```bash
# ① 最快：技能自己报（同时校验 5.11 红线）
./run.sh deps check
# ② 命令行读安装包
defaults read /Applications/WorkBuddy.app/Contents/Info.plist CFBundleShortVersionString
# ③ 界面：客户端左下角头像（菜单里带当前版本号）
```

### 升级到 5.11+（5.11 目前是灰度版本，这一步是新装唯一的卡点）

官方更新日志里 **5.11.0 发布于 2026-11-03，标注「灰度中」**
（<https://www.workbuddy.cn/docs/workbuddy/Changelog>），所以**官网下载页和
「历史版本下载」页最新只到 5.9.3** —— 照着官网下载就是那个跑不起来的版本。
两条可用的路：

**方式一：客户端内升级**（灰度按账号放开，可能提示「已是最新」）

```
客户端左下角头像 → 检查更新
```

**方式二：官方安装包覆盖安装**（拿不到灰度包时用这条；链接为官方 CDN，
2026-11-15 实测 HTTP 200、`application/x-apple-diskimage`，可直接装）

| 芯片 | 下载地址 | 体积 |
|---|---|---|
| Apple 芯片（M 系列） | <https://download.codebuddy.cn/workbuddy/saas/darwin-arm64/WorkBuddy-darwin-arm64-5.11.3.43806934-ecace0f7.dmg> | 约 358 MB |
| Intel | <https://download.codebuddy.cn/workbuddy/saas/darwin-x64/WorkBuddy-darwin-x64-5.11.3.43806934-ecace0f7.dmg> | 约 390 MB |

装的时候有三个坑（前两条是官方安装指南原话）：

1. **必须拖进「应用程序」**——双击 .dmg 后直接把 WorkBuddy 从磁盘映像里运行，
   后续无法自动更新。
2. 从磁盘映像里运行时若提示无法自动更新，按提示点「移动到应用程序」。
3. **退出旧版再启动**：`⌘Q`，并确认菜单栏图标也退干净，否则新版本不会接管；
   装完回客户端左下角头像核对版本号 ≥ 5.11.0，再跑 `./run.sh deps check`。

> 查芯片：屏幕左上角 苹果菜单 → 「关于本机」 → 「芯片」写 Apple M* 就是 ARM64，写 Intel 就是 x64。
> 官方安装指南：<https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Installation-Mac-Guide>

### 其余硬依赖（缺一条就跑不通，装之前先对一遍）

| 前置 | 要求 | 不满足会怎样 |
|---|---|---|
| 操作系统 | **macOS 12 (Monterey) 及以上**（WorkBuddy 官方要求） | 客户端本身起不来 |
| 平台 | **仅 macOS**。用了 `launchd` / `launchctl` / `lsof` / `defaults` / `pgrep` / `osascript` | Windows / Linux 无法使用，别装 |
| WorkBuddy 安装位置 | 默认 `/Applications/WorkBuddy.app`；装在别处需 `WORKBUDDY_APP=/path/WorkBuddy.app` | 版本读不到、日志里客户端显示「未运行」 |
| 客户端登录态 | 已登录，且**主窗口存在**（可最小化，不能关窗） | 成长中心在登录态里；没有窗口就没有可连的 webContents |
| 界面语言 | **简体中文** | 50+ 处按中文文案定位（「成长计划」「立即领取」「兑换」「使用」「抽奖(N)」…），只有成长中心入口可配 `[growth].open_labels`，其余换英文就点不到 |
| Python | **≥ 3.11**（`tomllib` / `sqlite3` / `zoneinfo`） | `run.sh` 直接退出 |
| Node.js | **≥ 22** | `tools/cdp_client.mjs` 用全局 `WebSocket`，`agent-browser` 自身也声明 `engines.node >= 22`；20/21 上连不上 iframe |
| agent-browser | `0.42.x`（`cd tools && npm install`） | 打不开成长中心页面 |
| CDP 调试端口 | 客户端带 `WORKBUDDY_REMOTE_DEBUGGING_PORT` 启动 | `deps check` 报 `WorkBuddy CDP MISSING` |
| 网络 | 能访问 `workbuddy.cn`（成长中心是服务端数据）；`npm install` 需 registry | 签到状态读不到 / 装不上依赖 |
| 可写目录 | `~/.local/share/buddy-web-checkin/`、`~/Library/LaunchAgents/` | 账本与登录项写不进去 |
| 权限 | **不需要 sudo**，也不需要辅助功能/录屏授权（可选件 `agent-desktop` 才需要） | — |


## 工作原理

```
WorkBuddy 客户端 (Electron)
  │  WORKBUDDY_REMOTE_DEBUGGING_PORT=9222
  │  → 主进程 appendSwitch('remote-debugging-port')
  ▼
CDP 127.0.0.1:9222
  ├── [page]   客户端主 renderer      ← agent-browser 跨 webContents 点「成长计划」→「立即前往」
  └── [iframe] growth-center (OOPIF)  ← tools/cdp_client.mjs 直连执行 JS：读状态 / 点按钮
```

关键点（2026-11-12 实测）：

- 客户端**不认** `--remote-debugging-port` 启动参数，只认环境变量 `WORKBUDDY_REMOTE_DEBUGGING_PORT`
- 打开成长中心的入口按钮不在主 renderer 的 DOM 里，只有 agent-browser 的聚合快照能点到
- 成长中心是独立 CDP target（`type=iframe`），可直连 WebSocket 执行 `Runtime.evaluate`
- `Target.createTarget` 被 Electron 生产版禁用，不能 `tab new`

## 安装（新机器 / 另一台机器）

顺序照抄即可，每一步都有可验证的输出。

```bash
# 0. 先确认客户端版本 ≥ 5.11（5.9.x 是网页版活动中心，装了也跑不起来）
defaults read /Applications/WorkBuddy.app/Contents/Info.plist CFBundleShortVersionString
# 不是 5.11+ → 回到上一节「升级到 5.11+」，升完再往下

# 1. 代码
git clone git@github.com:timesky/t-buddy-checkin.git
cd buddy-web-checkin

# 2. npm 依赖（agent-browser 必需，agent-desktop 可选）
cd tools && npm install && cd ..

# 3. 配置
cp config.example.toml config.toml

# 4. 开 CDP 调试端口（关键，必须做一次）
./run.sh ports status            # 只读：客户端是否已暴露端口、端口归谁
./run.sh install-launchd         # 写 daemon plist + 端口持久化（登录项）
# 端口没开时（status 显示无监听）：
./scripts/launch-workbuddy-debug.sh   # 写端口 → 优雅重启客户端 → 等 CDP 就绪

# 5. 验证（六项全绿才算装好）
./run.sh deps check      # 客户端版本 / Python / node / agent-browser / CDP / 可选件
./run.sh inspect         # 应看到 targets 里有 WorkBuddy + 签到状态
```

`deps check` 的第一项就是客户端版本，装好后输出应该长这样（6 项全 `OK`，
`agent-desktop` 显示「可选-未装」也不影响）：

```
  [OK       ] WorkBuddy 客户端 v5.11.3
       客户端 5.11.3 在支持范围内（≥5.11.0，实测基线 5.11.3）
  [OK       ] Python v3.11.15
  [OK       ] node v22.22.2
  [OK       ] agent-browser v0.42.0
  [OK       ] WorkBuddy CDP
       已连通（端口来源 active_file）
  [OK       ] agent-desktop v1.2.0
✅ 所有必需依赖就绪，可以运行 ./run.sh once / daemon。
```

版本不够时它会直接把升级方式打出来，并让 `checkin-now` / `once` / `daemon`
这类命令**在动手前就退出**（退出码 `3`），不会白跑一天。

### 端口是自适应的（不用再手填 9222）

客户端启动时只读一次环境变量 `WORKBUDDY_REMOTE_DEBUGGING_PORT`（只认纯数字），
然后 `appendSwitch('remote-debugging-port', n)`，**端口被占没有任何回退**。所以
本技能不再假设端口，按下面的优先级取真实值（`src/buddy_checkin/ports.py`）：

| 级别 | 来源 | 说明 |
|---|---|---|
| 1 | `~/.workbuddy/app/session/DevToolsActivePort` | Chromium 自己写的**当前**端口，再用 `/json/version` 的 UA 复核归属 |
| 2 | gui 域环境变量 | 「下次启动的意图」，文件陈旧时用它 |
| 3 | `[growth].cdp_port` | 回退值（`9222`） |
| 4 | 区间扫描 `9222–9242` | 兜底：`lsof` 可执行路径落在 `WorkBuddy.app/Contents/MacOS/` 才算 |

```bash
./run.sh ports status                 # 端口全貌 + 判定（跨机器排查直接贴这段）
./run.sh ports negotiate              # dry-run：该不该换端口、换成哪个
./run.sh ports negotiate --apply      # 落盘：setenv + plist + config.toml（需重启客户端）
```

端口被别的东西占用（9222 是 Chrome 默认调试端口）时，`status` 会标成
`非 WorkBuddy ✗`，`negotiate` 从区间里挑空闲端口。工具**绝不自动重启客户端**
（会掐掉你正在用的会话），只写文件并提示重启。

端口持久化由 `com.local.workbuddy-cdp-env` 这个 LaunchAgent 负责（`install-launchd`
自动生成并 bootstrap）。只手动 `launchctl setenv` 的话，重启电脑就丢 —— 这正是
「昨天还好、今天端口没了」的根因。

遇到 `Bootstrap failed: 5: Input/output error`：通常是同 label 已加载，
先 `launchctl bootout gui/$(id -u)/com.local.buddy-web-checkin` 再 bootstrap。

## 签到语义（2026-11-14 更正，重要）

**签到只在主窗口左下角的「Buddy加油站」浮层**，点「立即领取」。之前一版说
「客户端已无签到按钮、签到 = 读活跃地图」是**错的**，原因：那个卡片是远端运营位
下发的 HTML，在 `app.asar` 里搜不到字符串，就被误判成"按钮没了"；而「活跃地图」
只记录今天有没有用过客户端，跟签到领积分是两套东西 —— 结果签到整条线一次都
没真正执行，却天天报 `confirmed=True`。

实测结构（5.11.3）：主 renderer 的 `div.daily-checkin` → 内部 `.wb-slot--avatar-top-combo`
的 **shadow DOM** 里才有卡片（light DOM 永远是空的，读 textContent 会以为浮层不存在）：

| 元素 | 含义 |
|---|---|
| `#h` / `#d` | 卡片标题「Buddy加油站」/ 副标题「每日可领 100 积分」 |
| `#go` + `data-track-props={"type":"checkin"}` | 签到按钮，点击后经 bridge 走 `claimDailyCheckin` |
| `#tt` | 轻提示，成功后出现「领取成功」 |
| `.dot[aria-label]` | 轮播点；领完后签到卡从轮播里消失 = 今日已领 |

判定口径（2026-11-15 更正）：**只认服务端元数据，不认界面文案**。

- 今日是否已签：`checkinData.checkin_dates` 里有没有今天（服务端按日期记账）。
  裸布尔 `today_checked_in` 降级为辅助——服务端跨零点还没翻日时它会残留昨天的
  `true`，只看它会把今天误判成已签、然后静默一整天。检测到「布尔为真但今天不在
  列表」时按「服务端未翻日」处理：等 30s 重试，最多 180s，绝不记终态。
- 界面文案（「尚未登场」「轻轻路过」「今日」「已签到」）全部不参与判定——实测同一格
  的文案会随悬停/加载状态变化。

漏签补不了签到：只能去成长中心日历点漏签格、消耗补登卡（`.gs-cal-cell.is-broken.is-makeup-clickable`，
**永不自动消耗**）。日历图例实测：已打卡是同一色系 4 阶（`--wb-heat-cell-1..4`，
alpha 0.18/0.36/0.58/0.82，深浅只代表活跃度），未打卡是纯白 `rgb(250,250,250)`，
与未来日期 `is-todo` **同色**——所以「未打卡」不能只看颜色，必须叠加 class。

补签判定不看签到元数据，只消费服务端下发的可操作入口，并加两道校验：

1. **渲染完整性**：格子数 ≥28、恰有一个 `is-today`、至少一个 `is-todo`、本月 1 号到今天
   每格都在。任一不满足 → 记 `pending` 重试，**绝不当作「无漏签」**（弹窗刚开时格子
   还没渲染完，曾会误判 done）。
2. **缺口交叉校验**：`本月已过天数 − 本月已打卡格数 = 缺口`；`当前连续登录 N 天`
   用来验证「已打卡格数不可能少于 N」。两者冲突或缺口 >0 但无可补格 → 不下终态，
   分别记 `pending` / `impossible`。

## 命令

```bash
./run.sh deps check        # 依赖检查（不消耗模型额度）
./run.sh inspect           # 只读诊断：CDP / 签到服务端状态 / 漏签 / 抽奖券 / 页面按钮
./run.sh status            # 当日任务账本 + 各任务下次检查 + 最近运行
./run.sh checkin-now       # 只跑每日签到
./run.sh makeup-now        # 只跑漏签补签（成长中心日历 + 补登卡）
./run.sh exchange-now      # 只跑兑换
./run.sh lottery-now       # 只跑抽奖
./run.sh travel-now        # 只跑派遣（领礼物→派遣→读真实倒计时）
./run.sh once              # 按账本跑完五个任务一遍（= 一次重启自检）
./run.sh verify            # 同 once：用真实状态纠正账本并补做未完成任务
./run.sh daemon            # 常驻：五个任务滚动，全终态静默到 23:59
./run.sh install-launchd   # 写 daemon plist + 端口持久化（--load 立即加载）
./run.sh ports status      # 端口全貌与判定（只读，跨机器排查贴这段）
./run.sh ports negotiate   # 协商端口（--apply 落盘，--port N 指定）
```

除 `deps` / `inspect` / `install-launchd` / `model-check` 外都要带 `BUDDY_FREE_MODEL=1`（免费模型闸门，plist 内已内置）。

## 五个任务与当日账本

| 任务 | 完成判据（可复核的证据） | 今天做不了（impossible） |
|---|---|---|
| checkin | `checkin_dates` 含今天（裸布尔只作降级兜底） | 活动未开启 |
| exchange | 兑换成功（奖励项变「已兑换」）或明确「本月已兑」 | — |
| makeup | 日历渲染完整 且 本月缺口 = 0（或已补完） | 有缺口但无可补格 / 补登卡为 0 |
| lottery | 「抽奖(N)」的券抽到 0（子页显示剩余 0） | 本来就没有券 |
| travel | 派遣次数用尽且没有可领奖励 | — |

执行顺序：**checkin → exchange → makeup → lottery → travel**。兑换排在补签前面，
因为补登卡来自兑换奖励，先拿卡才能当轮补签（2026-11-15 调整，原顺序是先补后兑）。

账本 `~/.local/share/buddy-web-checkin/progress.json` 记录每个任务的状态：

- `pending` 没成功，按 60s → 120s → 300s → 600s 退避重试
- `done` / `impossible` 今天结束，不再尝试
- **五个任务全部终态** = 当日 100% 完成且无法继续 → 静默到 23:59
- 次日北京时间 0 点账本翻页，全部重置
- **重启不受静默控制**：进程启动时五个任务立刻用真实状态复核一遍，账本写错的
  `done` 会被改回 `pending` 并立即补做（已实测：把账本全改成 done 后重启，
  travel 被纠正回 pending）

派遣是唯一按真实倒计时滚动的任务：每次都从页面读「旅行倒计时 HH:MM:SS」，
到点先领奖励再派下一次，绝不写死间隔；页面提示次数用尽才记为今天结束。

## 幂等与安全边界

- 所有领取经 `state.claim_once` 幂等，重启不重复领
- 漏签补签**会消耗补登卡**（按需求开启）；卡为 0 时当天不再尝试，绝不凭空补
- 不点「去完成」类任务、不自动登录、不填账号密码、不绕验证码
- 不改客户端登录态，不读取/解密任何 token；日志与 `discovered.json` 走敏感键脱敏
- CDP 不可达时明确报错并给出修复命令，不静默降级到其它通道
- 任务结束后关闭自己打开的成长中心弹窗；用户手动打开的页面不动（`[growth].close_page_after_action`，默认 true）

## 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `deps check` 第一项 WorkBuddy 客户端报 MISSING | 版本 < 5.11（5.9.x 是网页版活动中心）→ 按输出里的两种升级方式升到 5.11+ |
| 命令直接退出、退出码 3 | 同上：版本红线拦住了，stderr 里有完整升级指引 |
| `deps check` 报 node 缺失（但明明装了） | 版本 < 22（`cdp_client.mjs` 用全局 `WebSocket`）→ `brew install node@22` |
| 版本显示「未知」/ 客户端显示「未运行」 | 客户端不在 `/Applications` → `export WORKBUDDY_APP=~/Applications/WorkBuddy.app` 后重试 |
| `no growth entry found in client` | ①客户端不是简体中文界面 ②入口文案变了 → 改 `[growth].open_labels` |
| `deps check` 报 WorkBuddy CDP MISSING | 没设环境变量或没重启客户端 → 跑 `scripts/launch-workbuddy-debug.sh` |
| `cdp eval timeout` | 客户端卡住或端口被占 → 重启客户端后 `./run.sh inspect` |
| `growth-center iframe not found` | 页面被关掉了 → 跑任意 `*-now` 命令会自动从侧栏入口重新打开 |
| 签到一直 `ok=False` | 看 `inspect` 的「服务端已签」；为 false 说明点击没生效，检查浮层是否被关掉（技能会自动从用户菜单重新唤起） |
| 补签没做 | `inspect` 的「可补漏签 / 补登卡」；卡为 0 就是当天补不了，等下次活动发卡 |
| 抽奖没抽 | 券来自兑换连登奖励；`inspect` 看「抽奖券」，为 0 即今天没有 |
| 派遣 `dispatch not confirmed (state=scene_dialog)` | 场景弹窗没走完 → 查 `inspect` 的页面状态，必要时手动点一次 |
| 另一台机器入口文案不同 | 改 `[growth].open_labels`（如 `["成长计划"]`） |

## 变更记录

- **2026-11-15（兼容性前置）**：把「版本」变成一等公民。新增 `src/buddy_checkin/compat.py`
  集中放版本红线（≥5.11.0，实测 5.11.3）、官方下载直链与升级指引；`deps check`
  首项校验客户端版本、node 门槛从「装了就行」收紧到 **≥22**（`cdp_client.mjs`
  用全局 `WebSocket`）；`checkin-now`/`once`/`daemon` 在版本过低时**动手前退出**
  （退出码 3）；`ports status`/`inspect` 打印客户端版本与判定；客户端路径不再写死
  `/Applications`（`WORKBUDDY_APP` 可覆盖）。文档补齐「5.9.x 是网页版活动中心、
  不支持」的原因与 5.11 灰度期的下载方式。

- **2026-11-14（第二轮）**：改成「五任务 + 当日账本」模型。签到判据换成服务端
  `checkinData`（`onRefreshCheckin` 现场刷新），不再用「卡片在不在」推测；新增漏签
  自动补签（补签格只认 CDP 真实鼠标事件，JS `click()` 无效）、自动抽奖（抽奖是子页
  不是弹层）、兑换后必须复核奖励项变化；全部任务终态才静默，重启强制用真实状态复核账本。

- **2026-11-14**：更正签到通道 —— 签到一直在主窗口左下角「Buddy加油站」浮层（shadow DOM 内的远端 slot html），新增 `adapters/fuel_card.py`；`GrowthBrowser.checkin()` 降级为「活跃地图只读参考」。修正 11-12 的错误结论（当时因 `app.asar` 搜不到该文案就判定"按钮已取消"，导致签到线连着两天一次都没真正执行却报成功）。
- **2026-11-12**：改用 CDP 通道（agent-browser + 客户端调试端口）。签到从「点按钮」改为「读活跃地图状态」。删除 `adapters/desktop.py`（CuaDriverSigner）与 `adapters/growth.py`（GrowthCenter），CuaDriver 不再是依赖。
- **2026-11-10**：引入 agent-desktop 第二引擎（严格 headless，不抢焦点）。
- **2026-11-09**：首版，CuaDriver AX 通道。

## Git

仓库 `https://github.com/timesky/t-buddy-checkin`（`main` 分支）。`.gitignore` 排除 `config.toml`、`tools/node_modules/`、`*.sqlite3`、`last-checkin-result.json`、`screenshots/`、`*.png`。
