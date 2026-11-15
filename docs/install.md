<style>
  :root { --wb-max-width: 980px; }
  body { max-width: var(--wb-max-width); margin: 0 auto; padding: 0 24px 48px; }
  pre, table { max-width: 100%; overflow-x: auto; }
  img { max-width: 100%; height: auto; }
</style>

# 安装

[← 文档索引](README.md) · [兼容性](compatibility.md) · [端口](ports.md)

新机器 / 换机器照抄即可，每一步都有可验证的输出。全部命令在仓库根目录执行。

## 0. 对一遍前置

先过一眼 [兼容性 §3 其余硬依赖](compatibility.md#3-其余硬依赖) —— 尤其这三条最容易踩：

- **WorkBuddy ≥ 5.11.0**（≤ 5.9.x 是网页版活动中心，装了也白装）
- **Node ≥ 22**（不是 20，`cdp_client.mjs` 用全局 `WebSocket`）
- **简体中文界面 + 主窗口别关**（50+ 处按中文文案定位）

```bash
defaults read /Applications/WorkBuddy.app/Contents/Info.plist CFBundleShortVersionString
# 不是 5.11+ → 先看 docs/compatibility.md §2 升级，升完再往下
```

## 1. 代码

```bash
git clone git@github.com:timesky/t-buddy-checkin.git
cd buddy-web-checkin
```

## 2. npm 依赖

```bash
cd tools && npm install && cd ..
```

会装 `agent-browser`（必需，跨 webContents 聚合快照/点击）与 `agent-desktop`（可选兜底）。

## 3. 配置

```bash
cp config.example.toml config.toml
```

`config.toml` 已进 `.gitignore`（含机器相关路径与个人偏好）。默认值能直接跑，
只有这几处可能需要改：

| 配置 | 什么时候改 |
|---|---|
| `[growth].open_labels` | 侧栏入口文案不同（`no growth entry found in client` 时） |
| `[growth].cdp_port` | 固定端口（一般不用动，端口是自适应的） |
| `[growth].close_page_after_action` | 想让技能打开过的成长中心页面留着（默认 `true`，即用完关掉） |
| `[desktop]` 段 | 只有用可选件 `agent-desktop` 兜底时才需要 |

## 4. 开 CDP 调试端口（关键，必须做一次）

客户端**不认** `--remote-debugging-port` 启动参数，只认环境变量
`WORKBUDDY_REMOTE_DEBUGGING_PORT`，且端口被占**没有任何回退**。所以顺序是「先看，再协商，最后持久化」：

```bash
# ① 只读：客户端是否已暴露端口、端口归谁（跨机器排查贴这段）
./run.sh ports status

# ② 写 daemon plist + 端口持久化登录项（开机自启，重启电脑不丢）
./run.sh install-launchd

# ③ 若 ① 显示「无监听」：写端口 → 优雅重启客户端 → 等 CDP 就绪
./scripts/launch-workbuddy-debug.sh
```

`install-launchd` 不带参数只写文件不加载；想立刻挂上 daemon 加 `--load`。
端口细节（四级来源、归属判定、换端口流程）见 [ports.md](ports.md)。

## 5. 验证

```bash
./run.sh deps check      # 客户端版本 / Python / node / agent-browser / CDP / 可选件
./run.sh inspect         # targets 里应有 iframe growth-center，并打印签到服务端状态
```

`deps check` 全绿长这样（6 项，`agent-desktop` 显示「可选-未装」也不影响使用）：

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

`inspect` 里确认这几项再算装好：

- `targets` 有客户端 page + `[iframe] growth-center`
- 「今日是否在 checkin_dates」「连登天数」「今日格已签」「渲染完整性」四项都有值
- 没有 `cdp eval timeout` / `growth-center iframe not found`

最后跑一轮真实流程（会真的签到）：

```bash
BUDDY_FREE_MODEL=1 ./run.sh once
```

## 6. 开机自启（可选但推荐）

```bash
./run.sh install-launchd --load   # 写 plist 并立即 bootstrap
launchctl list | grep buddy-web-checkin   # 应能看到，PID 非 0
```

只手动 `launchctl setenv` 而不装这个登录项，重启电脑后端口会丢 —— 这正是「昨天还好、
今天端口没了」的根因。

## 卸载

```bash
launchctl bootout gui/$(id -u)/com.local.buddy-web-checkin 2>/dev/null
launchctl bootout gui/$(id -u)/com.local.workbuddy-cdp-env 2>/dev/null
rm -f ~/Library/LaunchAgents/com.local.buddy-web-checkin.plist \
      ~/Library/LaunchAgents/com.local.workbuddy-cdp-env.plist
rm -rf ~/.local/share/buddy-web-checkin     # 账本、日志、discovered.json
```

登录项删掉后，客户端的调试端口环境变量不再被设置，等于恢复原状。
