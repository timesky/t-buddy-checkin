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
launchctl setenv WORKBUDDY_REMOTE_DEBUGGING_PORT 9222
# 然后退出并重启 WorkBuddy 客户端（或直接跑下面的脚本，自动完成）
./scripts/launch-workbuddy-debug.sh

# 5. 验证
./run.sh deps check      # 五条依赖应全绿
./run.sh inspect         # 应看到 [iframe] growth-center + 签到状态
```

`launchctl setenv` 重启后失效。想开机自动生效，把
`~/Library/LaunchAgents/com.local.workbuddy-cdp-env.plist`（`scripts/` 里有模板）加载进登录项：

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.workbuddy-cdp-env.plist
```

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
./run.sh inspect           # 只读诊断：CDP / targets / 签到状态 / 页面按钮
./run.sh status            # 三条时间线的下次执行时间 + 最近运行
./run.sh checkin-now       # 只跑签到状态校验
./run.sh travel-now        # 只跑旅行线（领礼物→派遣→读真实倒计时）
./run.sh exchange-now      # 只跑兑换检查
./run.sh once              # 全量一轮
./run.sh daemon            # 常驻：三条时间线
./run.sh install-launchd   # 写 LaunchAgent plist（不自动加载）
```

除 `deps` / `inspect` / `install-launchd` / `model-check` 外都要带 `BUDDY_FREE_MODEL=1`（免费模型闸门，plist 内已内置）。

## 三条时间线

| 线 | 周期 | 成功后的处理 |
|---|---|---|
| checkin | 每天 0 点恢复，每小时轮询 | 确认已打卡 → 禁用到当天 23:59 |
| exchange | 每天 0 点恢复，每小时轮询 | 兑换成功 / 明确「本月已兑」→ 禁用到 23:59 |
| travel | 动态滚动 | 每次读页面真实倒计时设置下次检查 |

派遣实际时长每次不同（页面只提示 1~4 小时），所以绝不套固定间隔；派遣次数用尽时禁用到 23:00。

## 幂等与安全边界

- 所有领取经 `state.claim_once` 幂等，重启不重复领
- **不自动消耗补签卡**、不点「去完成」类任务、不自动登录、不填账号密码、不绕验证码
- 不改客户端登录态，不读取/解密任何 token；日志与 `discovered.json` 走敏感键脱敏
- CDP 不可达时明确报错并给出修复命令，不静默降级到其它通道
- 任务结束后关闭自己打开的成长中心弹窗；用户手动打开的页面不动（`[growth].close_page_after_action`，默认 true）

## 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `deps check` 报 WorkBuddy CDP MISSING | 没设环境变量或没重启客户端 → 跑 `scripts/launch-workbuddy-debug.sh` |
| `cdp eval timeout` | 客户端卡住或端口被占 → 重启客户端后 `./run.sh inspect` |
| `growth-center iframe not found` | 页面被关掉了 → 跑任意 `*-now` 命令会自动从侧栏入口重新打开 |
| 签到一直 `ok=False` | 今天还没对话 → 正常发一次消息即可；或看 `inspect` 的 `今日已打卡` |
| 派遣 `dispatch not confirmed (state=scene_dialog)` | 场景弹窗没走完 → 查 `inspect` 的页面状态，必要时手动点一次 |
| 另一台机器入口文案不同 | 改 `[growth].open_labels`（如 `["成长计划"]`） |

## 变更记录

- **2026-11-14**：更正签到通道 —— 签到一直在主窗口左下角「Buddy加油站」浮层（shadow DOM 内的远端 slot html），新增 `adapters/fuel_card.py`；`GrowthBrowser.checkin()` 降级为「活跃地图只读参考」。修正 11-12 的错误结论（当时因 `app.asar` 搜不到该文案就判定"按钮已取消"，导致签到线连着两天一次都没真正执行却报成功）。
- **2026-11-12**：改用 CDP 通道（agent-browser + 客户端调试端口）。签到从「点按钮」改为「读活跃地图状态」。删除 `adapters/desktop.py`（CuaDriverSigner）与 `adapters/growth.py`（GrowthCenter），CuaDriver 不再是依赖。
- **2026-11-10**：引入 agent-desktop 第二引擎（严格 headless，不抢焦点）。
- **2026-11-09**：首版，CuaDriver AX 通道。

## Git

仓库 `https://github.com/timesky/t-buddy-checkin`（`main` 分支）。`.gitignore` 排除 `config.toml`、`tools/node_modules/`、`*.sqlite3`、`last-checkin-result.json`、`screenshots/`、`*.png`。
