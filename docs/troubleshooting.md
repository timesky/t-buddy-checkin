<style>
  :root { --wb-max-width: 980px; }
  body { max-width: var(--wb-max-width); margin: 0 auto; padding: 0 24px 48px; }
  pre, table { max-width: 100%; overflow-x: auto; }
  img { max-width: 100%; height: auto; }
</style>

# 故障排查

[← 文档索引](README.md) · [端口](ports.md) · [兼容性](compatibility.md)

## 先按这个顺序跑

```bash
./run.sh deps check     # ① 六项依赖，第一项就是客户端版本
./run.sh inspect        # ② 只读诊断：CDP / targets / 签到服务端状态 / 页面按钮
./run.sh status         # ③ 账本：哪个任务卡在 pending，退避到什么时候
tail -30 ~/.local/share/buddy-web-checkin/daemon.log   # ④ 常驻进程日志
```

90% 的问题在前两步就能定位：`deps check` 管「装没装对」，`inspect` 管「连没连上、
页面状态是什么」。

## 按现象查

| 现象 | 原因 / 处理 |
|---|---|
| `deps check` 第一项 WorkBuddy 客户端报 MISSING | 版本 < 5.11（5.9.x 是网页版活动中心）→ 按输出里的两种升级方式升到 5.11+ |
| 命令直接退出、退出码 `3` | 同上：版本红线拦住了，stderr 里有完整升级指引 |
| `deps check` 报 node 缺失（但明明装了） | 版本 < 22（`cdp_client.mjs` 用全局 `WebSocket`）→ `brew install node@22` |
| 版本显示「未知」/ 客户端显示「未运行」 | 客户端不在 `/Applications` → `export WORKBUDDY_APP=~/Applications/WorkBuddy.app` 后重试 |
| `no growth entry found in client`（兑换/补签/抽奖/派遣四项一起 `pending`） | 该机器侧栏入口文案与 `[growth].open_labels` 不匹配。2026-11-16 起代码会把左下角浮层运营卡的「立即前往」作为最后兜底（仅在常规入口全失败后尝试，且必须验证 iframe 出现）。仍失败时跑 `./tools/node_modules/.bin/agent-browser snapshot -i` 看真实按钮文案，改 `[growth].open_labels`；详见 [变更记录](changelog.md) |
| 礼物没领、也没派新的一次 | 先看 `travel` 是不是被上面的入口问题卡住（账本 `progress.json` 里 `travel.state`）；再看 daemon.log 里 `claim=` / `dispatch=` 与 `due_at`。入口四项都 `pending` 时，签到仍会是 `done`（走浮层，不依赖成长中心），容易误判成「服务正常」 |
| `deps check` 报 `WorkBuddy CDP MISSING` | 没设环境变量或没重启客户端 → `./scripts/launch-workbuddy-debug.sh` |
| `cdp eval timeout` | 客户端卡住或端口被占 → 重启客户端后 `./run.sh inspect` |
| `growth-center iframe not found` | 页面被关掉了 → 跑任意 `*-now` 命令会自动从侧栏入口重新打开 |
| 签到一直 `ok=False` | 看 `inspect` 的「今日是否在 checkin_dates」；为 false 说明点击没生效，检查浮层是否被关掉（技能会自动从用户菜单重新唤起） |
| 签到整夜 `pending`，日志全是 `checkinData not found (slot not mounted)` | 客户端开了多个 `app.asar` 窗口，旧版会连到没有浮层的那个；或浮层被客户端卸载。2026-11-16 已修（探测选页 + 读不到先唤起浮层），确认 daemon 已重启加载新代码，详见 [签到判据 §5](checkin-semantics.md#5-多窗口与浮层卸载2026-11-16-漏签复盘) |
| daemon 好像没干活 | 先 `launchctl print gui/$(id -u)/com.local.buddy-web-checkin`（看 `state = running`），再 `tail ~/.local/share/buddy-web-checkin/daemon.log`；全终态时静默到 23:59 属正常 |
| 签到「浮窗说已签、日历说尚未登场」 | 正常：浮层是乐观展示，判定只认 `checkin_dates`，见 [签到判据](checkin-semantics.md) |
| 补签没做 | 看 `inspect` 的「可补漏签 / 补登卡」；卡为 0 就是当天补不了，等下次活动发卡 |
| 抽奖没抽 | 券来自兑换连登奖励；`inspect` 看「抽奖券」，为 0 即今天没有 |
| 派遣 `dispatch not confirmed (state=scene_dialog)` | 场景弹窗没走完 → 查 `inspect` 的页面状态，必要时手动点一次 |
| 重启电脑后端口没了 | 没装 `com.local.workbuddy-cdp-env` 登录项 → `./run.sh install-launchd` |
| `Bootstrap failed: 5: Input/output error` | 同 label 已加载 / 被 disabled / 无权限 → 先 `bootout` 再 `bootstrap`，详见 [ports.md](ports.md#常见错误) |
| 账本显示全 `done` 但今天明明没做 | 重启进程即可：启动会用真实状态复核，写错的 `done` 会被改回 `pending` 并补做 |

## 跨机器排查

换机器（比如另一台 Mac）出问题时，顺序固定：

```bash
defaults read /Applications/WorkBuddy.app/Contents/Info.plist CFBundleShortVersionString
./run.sh deps check
./run.sh ports status     # 这段最有用，直接贴回来即可判断端口归属
./run.sh inspect
```

最常见的两类差异：

1. **版本不同** —— 一台 5.11.3、一台 5.9.x（网页版活动中心），后者直接不支持。
2. **端口不同** —— 两台机器环境变量值可能不一样（实测一台 9222、一台 9223），
   所以不能写死端口；`ports status` 会把真实端口与归属标出来。

入口文案也可能不同（如只有「成长计划」），改 `[growth].open_labels`：

```toml
[growth]
open_labels = ["成长计划", "做任务赢积分", "任务中心"]
```

## 诊断输出里该看什么

`./run.sh inspect` 的关键字段：

| 字段 | 期望 | 异常含义 |
|---|---|---|
| `targets` | 有客户端 page + `[iframe] growth-center` | 缺 iframe：成长中心没打开或客户端版本过低 |
| 今日是否在 `checkin_dates` | `True`（签过之后） | `False` 且点击无报错：点击没生效，看浮层状态 |
| 连登天数 / 今日格已签 | 数字 / 布尔 | 与 `checkin_dates` 冲突：服务端未翻日，等 30s 重试 |
| 渲染完整性 `render_ok` | `True` | `False`：弹窗没渲染完，任务会记 `pending` 重试，不是 bug |
| `card_balance` | 补登卡余额 | `0`：今天补不了签 |
| 端口来源 | `active_file` / `env` / `config` / `scan` | 若是 `config`，说明前两级都没读到，建议 `install-launchd` |

## 拿不到更多信息时

`daemon.log` 里每次运行都有任务级证据；需要更细的现场就单独跑一次对应的 `*-now`，
它会把该任务的判定过程打到 stdout（例如补签会打印 `past_days / checked / gap / streak /
render_ok / card_balance`）。提 Issue 时贴这一段 + `ports status` 通常就够定位。
