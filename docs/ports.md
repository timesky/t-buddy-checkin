<style>
  :root { --wb-max-width: 980px; }
  body { max-width: var(--wb-max-width); margin: 0 auto; padding: 0 24px 48px; }
  pre, table { max-width: 100%; overflow-x: auto; }
  img { max-width: 100%; height: auto; }
</style>

# CDP 端口：自适应，不用手填 9222

[← 文档索引](README.md) · [安装](install.md) · [故障排查](troubleshooting.md)

## 为什么需要一层端口自适应

客户端启动时只读一次环境变量 `WORKBUDDY_REMOTE_DEBUGGING_PORT`（只认纯数字），
然后 `appendSwitch('remote-debugging-port', n)`。两个后果：

1. **端口被占没有任何回退** —— 客户端照常启动，但调试端口根本没起来。
2. **9222 是 Chrome 的默认调试端口**，装了 Chrome / AdsPower 之类很容易撞上。

所以本技能不假设端口，而是先探真实值再用。实现见
[`src/buddy_checkin/ports.py`](../src/buddy_checkin/ports.py)。

## 候选来源（按优先级）

| 级别 | 来源 | 说明 |
|---|---|---|
| 1 | `~/.workbuddy/app/session/DevToolsActivePort` | Chromium 自己写的**当前**端口，再用 `/json/version` 的 UA 复核归属 |
| 2 | gui 域环境变量（`launchctl print gui/$(id -u)`） | 「下次启动的意图」，文件陈旧时用它 |
| 3 | 进程监听（`lsof -a -p <客户端pid> -iTCP -sTCP:LISTEN`） | 直接问客户端进程在听哪些端口——客户端 5.11.3 起会**自己协商端口**（不完全吃环境变量），重启后可能换到区间外，这条兜住这种情况 |
| 4 | 本地缓存（上次 resolve 命中值） | 前面都没命中时 |
| 5 | `[growth].cdp_port` | 配置回退值（默认 `9222`）——可能过期，探测不过就淘汰 |
| 6 | 区间扫描 `9222–9242` | 兜底：`lsof` 可执行路径落在 `WorkBuddy.app/Contents/MacOS/` 才算 |

> `DevToolsActivePort` 由 **Chromium** 写，不是 WorkBuddy；AdsPower 等 Chromium 系工具也写
> 同名文件。所以**只读 `~/.workbuddy/app/session/` 下那一个**，绝不全局 `find`。
>
> 实测（5.11.3）：环境变量没生效时客户端会**自选一个可用端口**开 CDP 并写进
> DevToolsActivePort——所以 config 里的 `cdp_port` 只是偏好，不是硬约束；候选链
> 每个端口都要过探测校验，静态值过期会被自动淘汰，绝不会「后退到旧端口」。

## 端口归属怎么判

**第一判据永远是 HTTP 探测**（`GET /json/version`）：

- UA 里含 `workbuddy` → `owned`（本机客户端 CDP）
- UA 是别的（如 Chrome）→ `foreign`
- **非 200 应答（如 404）→ `foreign`，绝无例外** —— 客户端很多内部服务端口
  （实测 18488）同为 Electron 进程所监听，对 `/json/version` 回 404。
  2026-11-18 事故：曾有版本用「exe 属于 WorkBuddy」兜底把这些端口判成 CDP，
  导致 daemon 整轮任务全打 404。**exe 归属只在「200 但无 UA」时参与判定**，
  404 时不构成任何证据。

判定结果分三种：`owned`（本机客户端）、`foreign`（被别的程序占了）、`closed`（没人监听）。
`status` 会把每个候选端口标出来，被占的显示 `非 WorkBuddy ✗`。

## 命令

```bash
./run.sh ports status                 # 端口全貌 + 判定（只读，跨机器排查直接贴这段）
./run.sh ports negotiate              # dry-run：该不该换端口、换成哪个
./run.sh ports negotiate --apply      # 落盘：setenv + plist + config.toml（需重启客户端）
./run.sh ports negotiate --port 9241  # dry-run 指定端口，用来验证它是否可用
```

`negotiate` 从区间里挑空闲端口；`--port` 指定的端口若已被 `foreign` 占用会**拒绝并返回
`rc=1`**（实测：伪 Chrome CDP 占 9241 → 拒绝；9242 可用 → 可 pin）。

> ⚠️ **`negotiate --apply` 必须在归属判定正常的版本上执行**。它会写登录项 plist、
> gui 域环境变量和 config.toml 三处——2026-11-18 事故：旧版把客户端内部端口 18488
> 误判为可用 CDP 后 `--apply`，把它写进了三处持久化文件；电脑重启后客户端带着
> 18488 启动，CDP 绑定失败（端口被自己的内部服务占用），整个客户端无 CDP、全部
> 任务失败。升级后先 `./run.sh ports status` 确认内部端口显示 `非 WorkBuddy ✗` 再用。

工具**绝不自动重启客户端** —— 那会掐掉你正在用的会话。它只写文件，然后提示你重启。

## 持久化：为什么要一个 LaunchAgent

只手动 `launchctl setenv WORKBUDDY_REMOTE_DEBUGGING_PORT 9223` 的话，重启电脑就丢。
所以 `install-launchd` 会同时生成两个登录项：

| Label | 作用 |
|---|---|
| `com.local.workbuddy-cdp-env` | 登录时 `setenv` 端口，保证客户端下次启动拿到正确的值 |
| `com.local.buddy-web-checkin` | daemon 常驻进程本身（内含 `BUDDY_FREE_MODEL=1`） |

daemon 启动时还会再跑一次 `ensure_env()`，双保险。

## 换端口的完整流程

客户端已在跑、且端口被别人占了：

```bash
./run.sh ports status                  # 确认目标端口是 foreign
./run.sh ports negotiate --apply       # 写 setenv + plist + config.toml
# 客户端左下角头像 → 退出（⌘Q，确认菜单栏图标也退掉）
open -a WorkBuddy                      # 重新启动，此时才读新端口
./run.sh ports status                  # 应显示 owned
BUDDY_FREE_MODEL=1 ./run.sh once       # 一次自检
```

## 常见错误

| 报错 | 原因 | 处理 |
|---|---|---|
| `Bootstrap failed: 5: Input/output error` | 同 label 已加载 / 被 disabled / 无权限（三种成因，报错文案一样） | 先 `launchctl bootout gui/$(id -u)/com.local.buddy-web-checkin` 再 bootstrap；仍失败看 `launchctl print-disabled gui/$(id -u)` |
| `deps check` 报 `WorkBuddy CDP MISSING` | 没设环境变量，或设了但没重启客户端 | `./scripts/launch-workbuddy-debug.sh` |
| `status` 里 active_file 指向一个不存在的端口 | 客户端已退出，文件是上次的残留 | 启动客户端，或直接 `launch-workbuddy-debug.sh` |
| 端口是 `owned` 但 `inspect` 连不上 | 客户端卡住 | 重启客户端后 `./run.sh inspect` |
| 重启电脑后端口又没了 | 没装 `com.local.workbuddy-cdp-env` 登录项 | `./run.sh install-launchd` |
| 任务报 `CDP 18488 unreachable: HTTP Error 404` | 端口探到客户端内部服务端口上了（旧版归属误判），或登录项 plist 里写的就是内部端口 | 升级到含 404 修复的版本；`grep setenv ~/Library/LaunchAgents/com.local.workbuddy-cdp-env.plist` 确认写的是真 CDP 端口（如 9222/9223），不是 18488 |

## 权限边界（排查时容易误解）

agent shell（包括 WorkBuddy 自己拉起的终端）**没有 gui 域的写权限**：

- `launchctl setenv` → `Not privileged`
- `launchctl bootstrap gui/501` 任意合法 plist → `Bootstrap failed: 5`（最小样例也一样）

所以端口持久化不能靠「运行时 setenv」，只能靠登录项在用户会话里生效 —— 这就是上面
那个 `com.local.workbuddy-cdp-env` 存在的原因。
