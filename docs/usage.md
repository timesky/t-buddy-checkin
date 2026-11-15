<style>
  :root { --wb-max-width: 980px; }
  body { max-width: var(--wb-max-width); margin: 0 auto; padding: 0 24px 48px; }
  pre, table { max-width: 100%; overflow-x: auto; }
  img { max-width: 100%; height: auto; }
</style>

# 用法：命令、五个任务、当日账本

[← 文档索引](README.md) · [签到判据](checkin-semantics.md) · [架构](architecture.md)

## 命令

手动执行统一走 `BUDDY_FREE_MODEL=1 ./run.sh <command>`（免费模型闸门的显式确认，
plist 内已内置，常驻进程不用管）。

| 命令 | 作用 | 会动手吗 |
|---|---|---|
| `deps check` | 逐项检查依赖（安装后必跑） | 否，只读 |
| `inspect` | 只读诊断：CDP 连通 / targets / 浮层签到状态 / 漏签 / 抽奖券 / 页面按钮 | 否，只读 |
| `status` | 当日任务账本 + 各任务下次检查 + 最近运行 | 否，只读 |
| `checkin-now` | 每日签到一轮（点左下角浮层「立即领取」，用服务端状态确认） | 是 |
| `makeup-now` | 漏签补签（成长中心日历 + 补登卡） | 是，**消耗补登卡** |
| `exchange-now` | 兑换一轮 | 是，**消耗积分** |
| `lottery-now` | 抽奖一轮（抽到券为 0） | 是，**消耗抽奖券** |
| `travel-now` | 派遣一轮（领礼物 → 派遣 → 读真实倒计时） | 是 |
| `once` / `verify` | 按账本跑完五个任务（= 一次重启自检） | 是 |
| `daemon` | 常驻：五个任务滚动，全终态静默到 23:59 | 是 |
| `install-launchd` | 写 daemon plist + 端口持久化（`--load` 立即加载） | 写文件 |
| `ports status` | 端口全貌与判定（只读） | 否 |
| `ports negotiate` | 协商空闲端口（`--apply` 落盘，需重启客户端生效） | 写文件 |
| `model-check` | 查看免费模型策略与确认状态 | 否 |

退出码：`0` 正常 / `1` 失败 / `2` 缺 `BUDDY_FREE_MODEL=1` / `3` 客户端版本低于 5.11。

## 五个任务

| 任务 | 完成判据（可复核的证据） | 今天做不了（`impossible`） |
|---|---|---|
| `checkin` | `checkin_dates` 含今天（裸布尔只作降级兜底） | 活动未开启 |
| `exchange` | 兑换成功（奖励项变「已兑换」）或明确「本月已兑」 | — |
| `makeup` | 日历渲染完整 且 本月缺口 = 0（或已补完） | 有缺口但无可补格 / 补登卡为 0 |
| `lottery` | 「抽奖(N)」的券抽到 0（子页显示剩余 0） | 本来就没有券 |
| `travel` | 派遣次数用尽且没有可领奖励 | — |

**执行顺序固定**：`checkin → exchange → makeup → lottery → travel`。
兑换排在补签前面，因为补登卡来自兑换奖励 —— 先拿卡，才能当轮补签
（2026-11-15 调整，原顺序是先补后兑，导致有缺口却无卡可用）。

### 派遣是唯一按真实倒计时滚动的任务

页面只提示 1～4 小时，且每次不同。所以派遣后必须**重读**页面的「旅行倒计时 HH:MM:SS」
算出 `due_at`，**禁止写死间隔**。到点先领奖励再派下一次；页面提示「明天再来 / 今日已派完 /
次数已用完」才记为今天结束。

## 当日账本

账本文件：`~/.local/share/buddy-web-checkin/progress.json`。

- `pending`：没成功，按 **60s → 120s → 300s → 600s** 退避重试
- `done`：有可复核证据，今天不再尝试
- `impossible`：今天确实做不了，今天不再尝试
- **五个任务全部终态** = 当日 100% 完成且无法继续 → 静默到 23:59
- 次日北京时间 0 点翻页，全部重置

**重启不受静默控制**：进程启动时五个任务立刻用真实状态复核一遍，账本里写错的 `done`
会被改回 `pending` 并立即补做（已实测：把账本全改成 done 后重启，`travel` 被纠正回
`pending` 并重派）。

`./run.sh status` 就是账本的可读视图，包含每个任务的下次检查时间与最近一次运行的证据字段
（`checked_date` / `judge` / `gap` / `streak` 等）。

## 常驻与开机自启

```bash
BUDDY_FREE_MODEL=1 ./run.sh daemon      # 前台跑，调试用
./run.sh install-launchd --load         # 装成 LaunchAgent，登录即起
launchctl list | grep buddy-web-checkin # 确认在跑
tail -f ~/.local/share/buddy-web-checkin/daemon.log
```

daemon 与 `once` 的差别只有一个：`once` 跑完一轮就退出，`daemon` 会按账本滚动重试，
直到当天全部终态才静默。

## 幂等与安全边界

- 所有领取经 `state.claim_once` 幂等，重启不重复领
- 漏签补签**会消耗补登卡**（这是需求本身），卡为 0 时当天不再尝试，绝不凭空补
- 不点「去完成」类任务、不自动登录、不填账号密码、不绕验证码
- 不改客户端登录态，不读取/解密任何 token；日志与 `discovered.json` 走敏感键脱敏
- CDP 不可达时明确报错并给出修复命令，不静默降级到其它通道
- 任务结束后关闭**自己打开的**成长中心弹窗；用户手动打开的页面不动
  （`[growth].close_page_after_action`，默认 `true`）
