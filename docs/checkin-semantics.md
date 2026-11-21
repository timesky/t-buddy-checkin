<style>
  :root { --wb-max-width: 980px; }
  body { max-width: var(--wb-max-width); margin: 0 auto; padding: 0 24px 48px; }
  pre, table { max-width: 100%; overflow-x: auto; }
  img { max-width: 100%; height: auto; }
</style>

# 签到与补签判据

[← 文档索引](README.md) · [用法](usage.md) · [故障排查](troubleshooting.md)

这一页解释三件事：**签到按钮在哪**、**凭什么算签上了**、**漏签怎么补**。
判断口径经过 4 次更正，下面的结论都有实测依据。

## 1. 签到在哪

**签到只在主窗口左下角的「Buddy加油站」浮层**，点「立即领取」。

早先有一版结论是「客户端已无签到按钮，签到 = 读活跃地图」，那是**错的**：

- 那张卡片是**远端运营位下发的 HTML**，在 `app.asar` 里搜不到字符串，就被误判成「按钮没了」
- 「活跃地图」只记录今天有没有用过客户端，跟签到领积分是两套东西
- 结果：签到整条线一次都没真正执行，却天天报 `confirmed=True`

实测结构（5.11.3）：主 renderer 的 `div.daily-checkin` → 内部
`.wb-slot--avatar-top-combo` 的 **shadow DOM** 里才有卡片。light DOM 永远是空的，
读 `textContent` 会以为浮层不存在。

| 元素 | 含义 |
|---|---|
| `#h` / `#d` | 卡片标题「Buddy加油站」/ 副标题「每日可领 100 积分」 |
| `#go` + `data-track-props={"type":"checkin"}` | 签到按钮，点击后经 bridge 走 `claimDailyCheckin` |
| `#tt` | 轻提示，成功后出现「领取成功」 |
| `.dot[aria-label]` | 轮播点；领完后签到卡从轮播里消失 = 今日已领 |

浮层被关掉时，先从侧栏用户菜单重新唤起（`avatar_menu_fuel_station`），再点 `#go`。

## 2. 凭什么算签上了

**只认服务端元数据，不认界面文案。**

权威数据在 React fiber 上的 `AvatarTopComboSlot.checkinData`：

- `today_checked_in`（布尔）
- `today_credit`
- `streak_days`（当前连登天数）
- `checkin_dates`（服务端签到流水）
- `week_progress`

需要现场刷新时调 `onRefreshCheckin()` 再读一次。

**最终判据：`today in checkin_dates`。**

裸布尔被降级为辅助，原因很具体：服务端跨零点还没翻日时，`today_checked_in` 会残留昨天的
`true`，只看它会把今天误判成已签、然后静默一整天。检测到「布尔为真但今天不在列表」时按
**服务端未翻日**处理：每 30s 重试，最多 180s，**绝不记终态**（`_wait_rollover`）。

界面文案（「尚未登场」「轻轻路过」「今日」「已签到」）**全部不参与判定** —— 实测同一格
的文案会随悬停/加载状态变化。这也是为什么「浮窗显示已签到、日历显示尚未登场」这类
表象不一致不影响判定结果。

> 顺带更正一个直觉：**签到不是 24 小时重置**。实测 11-14 08:33 → 11-15 00:00:02，
> 间隔 15.5h 也被接受 —— 是按自然日翻页，浮层的「已签到」只是乐观展示。

### checkinData 是前端乐观态（2026-11-20 假成功事故）

点击签到后浮层回显的 `today_credit / streak_days / checkin_dates` 全是**前端乐观
更新**，服务端可能实际没落库。2026-11-20 本机实测：00:00:02 判定
`claimed: credit=100, streak=42d`，次日 makeup 却用补签卡补了 11-20 ——
服务端当天根本没记上，浮层一整天都显示「已签到」。浮层显示的连签天数也与
服务端真值不符（补签后服务端重算连签，用户看到 20 天，浮层回显 43 天）。

所以「claimed」只是**强旁证，不是服务端回执**。防线有三层：

1. 夜检（默认 22:00，`[schedule].evening_check_at`）：全终态后不当晚静默到
   午夜，22:00 唤醒做一次全量复核；
2. makeup 的日历缺口交叉：若当天实际没签上，缺口计算会发现「缺口 > 0 但
   今日格不可补」，makeup 记 `pending` 而不是 `no missed day` —— 假成功
   当晚就暴露，不再静默躺平；
3. 次日 0 点翻页后 makeup 立即补昨天（消耗补签卡的最后兜底）。

## 3. 漏签怎么补

漏签补不了签到，只能去**成长中心日历**点漏签格、消耗**补登卡**：
`.gs-cal-cell.is-broken.is-makeup-clickable`，`dt-remark` 里带 `date` 与 `cardBalance`。
点开 `.gs-cell-toast--makeup` 再点「使用」消耗一张卡。

**这些格子只认 CDP 真实鼠标事件**（`Input.dispatchMouseEvent`），JS 的 `element.click()`
点了没有任何反应（必须是 trusted event，且要先 `scrollIntoView`）。

### 日历图例实测（2026-11-15 采样）

| 状态 | 颜色 | 说明 |
|---|---|---|
| 已打卡 | 同一色系 4 阶 `--wb-heat-cell-1..4`，alpha `0.18 / 0.36 / 0.58 / 0.82` | 深浅只代表活跃度，都算已签 |
| 未打卡（可补登） | 纯白 `rgb(250,250,250)` | **与未来日期 `is-todo` 同色** |
| 今日已签 | `rgb(111,229,198)`，alpha = 1 | 实心 |

所以「未打卡」**不能只看颜色**，必须叠加 class 判断，否则会把未来日期也算成漏签。

### 两道校验（缺一不可）

补签判定不看签到元数据，只消费服务端下发的可操作入口，另加两道校验：

1. **渲染完整性**：格子数 ≥ 28、恰有一个 `is-today`、至少一个 `is-todo`、本月 1 号到今天
   每格都在。任一不满足 → 记 `pending` 重试，**绝不当作「无漏签」**
   （弹窗刚开时格子还没渲染完，曾因此误判 done）。
2. **缺口交叉校验**：`本月已过天数 − 本月已打卡格数 = 缺口`；`当前连续登录 N 天`
   用来验证「已打卡格数不可能少于 N」。两者冲突，或缺口 > 0 但无可补格 →
   分别记 `pending` / `impossible`，不下终态。

补完之后**逐格复验 alpha**，确认目标格真的变实心才算 done，不看 toast 文案。

## 4. 已知边界

- `checkin_dates` 是**不完整的签到流水**（补签不写回该列表），所以它能判「今天」，
  不能用来反推历史缺口 —— 历史缺口只从日历算。
- 「今日格 alpha == 1 即已签」只有已签样本，没有未签反例。选了**阻断**策略：
  误判只会让任务反复 `pending`，不会漏签。
- 补登卡余额来自页面 `cardBalance`，不本地记账（避免与活动规则不同步）。

## 5. 多窗口与浮层卸载（2026-11-16 漏签复盘）

**现象**：daemon 从 00:00 到 10:58 每 10 分钟重试一次，共 69 次全部报
`checkinData not found (slot not mounted)`，整天没签到；而人工跑 `inspect` 却能读到
服务端状态。常驻进程本身一直在跑，登录项也是 `running`。

**两个叠加的原因**：

1. **选错 renderer**。客户端会同时开多个 `app.asar` 页面（主窗口、表格编辑器、
   第二个 WorkBuddy 窗口），只有挂了浮层的那个 shadow DOM 里有 `#go`。旧的
   `page_ws()` 取 `/json/list` 里第一个 URL 匹配项，窗口顺序一变就连到没有浮层的
   页面，`checkinData` 自然读不到。
2. **唤起顺序反了**。`claim()` 先读状态，读不到就立刻返回失败，而
   `ensure_visible()` 排在后面 —— 浮层一旦没挂载，就陷入「读不到 → 不唤起 →
   继续读不到」的循环，重试 69 次也不会自愈。

**修复**（`adapters/fuel_card.py`）：

- `page_ws()` 逐个探测候选页，选中真正含 `#go` 的 renderer 并缓存；CDP 层失败或
  状态报 `slot not mounted` 时清缓存重新扫描。
- 所有页面都没有浮层时，回退页优先取标题为 `WorkBuddy` 的主窗口（跳过表格窗口），
  因为从用户菜单重新唤起浮层必须点在主 renderer 上。
- `claim()` 读不到状态且 `host=false` 时，先重新选页 + `ensure_visible()` 唤起浮层，
  再读一次状态；仍读不到才记 `pending`。失败证据里带 `reopen` 字段区分
  `reopened` / `reopen_failed` / `not_needed`，便于从日志定位。

**自检**（只读，不改任何状态）：

```bash
./run.sh inspect                       # 看「今日是否在 checkin_dates」
PYTHONPATH=src python3 -c "
from buddy_checkin.adapters.fuel_card import FuelCard
fc = FuelCard({'growth': {'cdp_port': 9222}})
print(fc.page_ws(), fc.status(refresh=False))"
```

实现见 [`adapters/fuel_card.py`](../src/buddy_checkin/adapters/fuel_card.py)（浮层签到）与
[`adapters/growth_browser.py`](../src/buddy_checkin/adapters/growth_browser.py)（日历扫描与补签）。
