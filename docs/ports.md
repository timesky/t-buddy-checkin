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

## 四级来源（按优先级）

| 级别 | 来源 | 说明 |
|---|---|---|
| 1 | `~/.workbuddy/app/session/DevToolsActivePort` | Chromium 自己写的**当前**端口，再用 `/json/version` 的 UA 复核归属 |
| 2 | gui 域环境变量（`launchctl print gui/$(id -u)`） | 「下次启动的意图」，文件陈旧时用它 |
| 3 | `[growth].cdp_port` | 配置回退值（默认 `9222`） |
| 4 | 区间扫描 `9222–9242` | 兜底：`lsof` 可执行路径落在 `WorkBuddy.app/Contents/MacOS/` 才算 |

> `DevToolsActivePort` 由 **Chromium** 写，不是 WorkBuddy；AdsPower 等 Chromium 系工具也写
> 同名文件。所以**只读 `~/.workbuddy/app/session/` 下那一个**，绝不全局 `find`。

## 端口归属怎么判

两个判据任一成立即认定是本机 WorkBuddy：

- `GET /json/version` 的 UA 里含 `workbuddy`
- 监听进程的 `lsof -p PID -a -d txt` 可执行文件在 `WorkBuddy.app/Contents/MacOS/` 下

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

## 权限边界（排查时容易误解）

agent shell（包括 WorkBuddy 自己拉起的终端）**没有 gui 域的写权限**：

- `launchctl setenv` → `Not privileged`
- `launchctl bootstrap gui/501` 任意合法 plist → `Bootstrap failed: 5`（最小样例也一样）

所以端口持久化不能靠「运行时 setenv」，只能靠登录项在用户会话里生效 —— 这就是上面
那个 `com.local.workbuddy-cdp-env` 存在的原因。
