<style>  
  :root { --wb-max-width: 980px; }  
  body { max-width: var(--wb-max-width); margin: 0 auto; padding: 0 24px 48px; }  
  pre, table { max-width: 100%; overflow-x: auto; }  
  img { max-width: 100%; height: auto; }  
</style>

# buddy-web-checkin

WorkBuddy 每日活跃（签到）/ 领礼物 / 派遣 / 兑换自动化。全部走 **CDP**：直连 WorkBuddy 客户端内嵌的成长中心页面做 DOM 级操作，不激活窗口、不移动光标、不抢鼠标键盘。

零第三方 Python 包（仅标准库 `tomllib` / `sqlite3` / `zoneinfo`）。运行时依赖：`node` + `agent-browser` + 客户端调试端口。旧 CuaDriver / AX 通道已整体移除。

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

```bash
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

# 5. 验证
./run.sh deps check      # 五条依赖应全绿
./run.sh inspect         # 应看到 [iframe] growth-center + 签到状态
```

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

判定口径：**看到签到卡才算可领；看到「领取成功」或签到卡消失才算已签**。
浮层不在（被关闭/未登录/未下发）一律视为「未知」，不写 `last_checkin_day`。

漏签补不了签到：只能去成长中心日历点漏签格、消耗补登卡（`.gs-cal-cell.is-broken`，
**永不自动消耗**）。活跃地图（`.gs-cal-cell.is-today` 实心绿 `rgb(111,229,198)`）
仅作参考，不再作为签到判据。

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
| checkin | 服务端 `today_checked_in=true`（含 `checkin_dates` 有今天） | 活动未开启 |
| makeup | 日历里没有可补漏签格 | 还有漏签但补登卡为 0 |
| exchange | 兑换成功（奖励项变「已兑换」）或明确「本月已兑」 | — |
| lottery | 「抽奖(N)」的券抽到 0（子页显示剩余 0） | 本来就没有券 |
| travel | 派遣次数用尽且没有可领奖励 | — |

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
| `deps check` 报 WorkBuddy CDP MISSING | 没设环境变量或没重启客户端 → 跑 `scripts/launch-workbuddy-debug.sh` |
| `cdp eval timeout` | 客户端卡住或端口被占 → 重启客户端后 `./run.sh inspect` |
| `growth-center iframe not found` | 页面被关掉了 → 跑任意 `*-now` 命令会自动从侧栏入口重新打开 |
| 签到一直 `ok=False` | 看 `inspect` 的「服务端已签」；为 false 说明点击没生效，检查浮层是否被关掉（技能会自动从用户菜单重新唤起） |
| 补签没做 | `inspect` 的「可补漏签 / 补登卡」；卡为 0 就是当天补不了，等下次活动发卡 |
| 抽奖没抽 | 券来自兑换连登奖励；`inspect` 看「抽奖券」，为 0 即今天没有 |
| 派遣 `dispatch not confirmed (state=scene_dialog)` | 场景弹窗没走完 → 查 `inspect` 的页面状态，必要时手动点一次 |
| 另一台机器入口文案不同 | 改 `[growth].open_labels`（如 `["成长计划"]`） |

## 变更记录

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
