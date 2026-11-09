<style>
  :root { --wb-max-width: 980px; }
  body { max-width: var(--wb-max-width); margin: 0 auto; padding: 0 24px 48px; }
  pre, table { max-width: 100%; overflow-x: auto; }
  img { max-width: 100%; height: auto; }
</style>

# buddy-web-checkin

独立的每日签到 / 礼物兑换 / 派遣自动化技能。**只有每日签到依赖 WorkBuddy 桌面客户端**，其余全部在网页端完成；两条通道都在后台隔离执行，**不弹窗、不激活窗口、不抢鼠标键盘、不碰你正在用的浏览器**。

零第三方 Python 包（仅标准库 `tomllib` / `sqlite3` / `zoneinfo`）。外部 CLI 均为 Apache-2.0：`agent-browser`（已随 WorkBuddy 安装）、可选 `agent-desktop`。不读取、不解密、不导出任何客户端登录态或令牌。

## Git 管理

代码托管于 `https://github.com/timesky/t-buddy-checkin`（`main` 分支），本机通过 SSH key 读写（`~/.ssh/id_rsa_tangming` 与 `lcc_op_rsa`，均为 `timesky` 身份，已验证可推送）。

- 远程为空仓库，首次推送即创建 `main`：`git push -u origin main`。
- 受 `.gitignore` 控制的本地产物**不入库**：`config.toml`（含机器路径）、`tools/node_modules/`（agent-desktop 依赖）、`login-qr.png`、`last-checkin-result.json`、`discovered.json`、`progress.txt`、`*.command`、`*.sqlite3`、浏览器登录态。
- 新克隆后：`cp config.example.toml config.toml` 按需改；`tools/node_modules` 仅在使用 agent-desktop 引擎时需另装（`npm install --prefix tools agent-desktop`），当前默认 cua-driver 不需要。

## 免费模型约束（重要）

本技能**依赖 WorkBuddy**：每日签到在 WorkBuddy 客户端内、成长中心是 WorkBuddy 个人中心。作为 WorkBuddy 技能 / 定时任务运行时，会消耗模型额度，**必须运行在「免费模型」下**。

- 每次执行前（含定时器触发），先在 WorkBuddy 模型选择器选「免费模型」。
- 命令行以环境变量 `BUDDY_FREE_MODEL=1` 显式确认已选免费模型，否则命令**拒绝执行**（退出码 2）。
- `./run.sh install-launchd` 生成的 plist 已内置 `BUDDY_FREE_MODEL=1`，定时任务无需额外操作。
- 查看策略：`run.sh model-check`。

说明：脚本无法读取当前选中的模型，故以运维侧显式确认作为放行闸门，避免误在付费模型下运行。

## 签到通道（2026-11-09 真实跑通）

系统级 TCC 数据库给出决定性答案：`com.trycua.driver` 是 `auth=2`（已授权），而 `com.apple.Terminal` 和 `com.tencent.workbuddy.mac` 都是 `auth=0`。所以正解是 **CuaDriver**（第一轮就提到的 computer use），不是 agent-desktop。

`config.toml` 已设 `desktop.engine = "cua-driver"`，流程：

1. `cua-driver call list_windows` 找 WorkBuddy 可见主窗口（实测 `pid=452` / `window_id=66`）。
2. `get_window_state` 重新索引，定位 Buddy 加油站卡片里的 `AXButton "立即领取"`（实测 `element_index=65`）。
3. `click` 用 `element_index` 走 AX press —— **不移动光标、不抢焦点、不激活窗口**。
4. 再取一次快照复核：按钮与卡片消失即签到成功。

实测结果：点击后「Buddy加油站 / 每日可领 100 积分 / 立即领取」全部从 AX 树消失；随后 `./run.sh checkin-now` 返回 `{"ok": true, "already": true}`，并落库 `last_checkin_day=2026-11-09`。按钮不存在时适配器判定为「今天已签到」，不会重复点击。

前置条件：CuaDriver daemon 必须在自身身份下运行：

```bash
/Applications/CuaDriver.app/Contents/MacOS/cua-driver permissions grant
/Applications/CuaDriver.app/Contents/MacOS/cua-driver permissions status --json   # accessibility: true
```

`permissions grant` 会通过 LaunchServices 以 `com.trycua.driver` 身份启动 daemon（`responsible_ppid=1`），之后 `checkin-now` / `once` / `daemon` 都复用它。

## 成长中心：领礼物 / 派遣 / 签到兑换（2026-11-09 真实跑通）

通道：用 CuaDriver 的 AX 能力读**已登录的 Edge「个人中心」**（`/profile/growth-center`）。不用注入 cookie，也不碰需要 Bearer token 的签到接口。

| AX 状态 | 页面按钮 | 工具动作 |
|---|---|---|
| `idle` | `派猫猫旅行` | 点开场景弹窗 → `确定派出` |
| `traveling` | `旅行倒计时 00:59:56` | 解析真实剩余秒数 → 到期时间戳 |
| `claimable` | `领取礼物` | 打开归来弹窗 → `领取 7 积分` → 复核 `已领取` |
| `gift_dialog` | `领取 N 积分` | 直接领取并复核 |
| `scene_dialog` | `确定派出` | 确认派遣后立刻重读倒计时 |

**派遣时长动态处理（关键）**：派遣前页面只写「预计出行时间为1～4小时」，实际每次不同。工具在**派遣之后**从 `旅行倒计时 HH:MM:SS` 读真实剩余秒数，`due_at = now + remaining + claim_offset_sec`（默认 3 秒，防服务端未结算），写入 SQLite。`daemon` 精确等到期 → 领礼物 → 再派遣 → 立刻读新的倒计时 → 生成下一次 due。每轮都按当次实际时长算，不套固定间隔；关机错过的到期在启动补偿时也会重新读页面现状。

**签到兑换（保守 + 幂等）**：`兑换奖励`（连登里程碑）会弹出「选择奖励」，只点真正已解锁的档位；当前是「还差 1 / 8 / 22 天」就自动关弹窗并跳过。`抽奖(N)` 仅 N>0、`开启盲盒 a/b` 仅能量满（a≥b）才点；**补登卡和日期按钮是有限资源，永不自动消耗**。每项按「北京时间日期 + 类型 + 文案」幂等去重。

实测 `./run.sh once`：签到已签 → 礼物无可领（旅行中）→ `milestone: no unlocked reward yet` → `traveling; due from real countdown (2912s)`，`travel_due_at = 2026-11-09 17:29:17 +0800`。

## 架构

```
每日签到 ──> desktop 通道 ──> agent-desktop 无障碍树 ref（首选，后台）
                          └─> CDP 连 Electron 渲染进程，eval 穿透 Shadow DOM（备选）
                          └─> computer-use 截屏点坐标（默认禁用，会抢焦点）

礼物兑换 / 派遣 / 到期领奖 ──> web 通道 ──> agent-browser
                                          headless + --session 隔离 + --profile 独立登录态

精确到期 ──> 读 end_at / remaining_seconds / 页面倒计时
         ──> 换算成北京时间绝对时间戳，写入 SQLite
         ──> heapq 调度器单次等到期 ──> 领奖 ──> 立刻再派遣 ──> 生成下一次到期
```

时间口径：**统一北京时间**（`Asia/Shanghai`）。带时区的 ISO 正常转换；naive 字符串按北京时间解释，绝不原样当 UTC。内部调度用 epoch 秒，不受本机时区影响。

## 快速开始

```bash
cd /path/to/buddy-web-checkin
cp config.example.toml config.toml      # 按需修改
chmod +x run.sh

./run.sh probe        # 只读探测：网页端入口 + 桌面端可用后端，写 discovered.json
./run.sh setup        # headless 打开登录页，把二维码截图存到 login-qr.png，手机扫码
./run.sh status       # 只读查看下次签到 / 旅行到期时间
./run.sh checkin-now  # 只做每日签到（桌面端），不碰网页端
./run.sh once         # 手动跑一轮完整闭环，确认无误
./run.sh daemon       # 常驻精确调度（Ctrl-C 退出）
./run.sh install-launchd   # 只写 LaunchAgent plist，不自动加载
```

`setup` 的扫码是**唯一需要你动手的登录动作**：headless 页面截图出二维码，登录态只保存在 `web.profile` 这个独立目录里，和你日常 Chrome 完全隔离。

## 复用 Edge 已登录状态（免扫码）

如果 Edge 里已经登录过 `www.workbuddy.cn`，可以不扫码：

```bash
# 必须在 macOS 终端里直接运行，不要经由 WorkBuddy / 沙箱会话
cd /Users/hy_002/WorkBuddy/2026-11-08-11-27-37/buddy-web-checkin
./run.sh setup-from-edge
```

`config.toml` 的 `web.executable_path` 已指向 Edge 可执行文件。命令把 Edge 源 profile 的 `Cookies`（sqlite 一致性快照）、`Preferences`、`Secure Preferences`、`Local Storage`、`Local State` **只读复制**到项目独立 profile，再 `agent-browser close --all` 冷启动（daemon 单例，不冷启动 `--executable-path` 会被忽略），最后用 Edge 打开 `/app` 验证 URL 是否还在 `/login`。必须用 Edge 自己的可执行文件：macOS 上 v10 cookie 由 Keychain 的 `Microsoft Edge Safe Storage` 派生密钥加密，Chrome 用的是 `Chrome Safe Storage`，跨浏览器读会解密失败。源 profile 全程只读、不改动，也不启动或激活你正在用的 Edge 窗口。

**实测边界（2026-11-09）**：Edge Default profile 确认有 `session` / `session_2` / `KEYCLOAK_SESSION` / `AUTH_SESSION_ID`（均 httpOnly，主 `session` 有效期到 11/15 11:44 北京时间）。但在 WorkBuddy 沙箱会话内执行时，`security find-generic-password -w` 读 Keychain 密码被 SIGTERM，Edge 启动时把解不开的 httpOnly 认证 cookie 全部丢弃，URL 仍落回 `/login`；手动 CDP 端口被代理拦成 502，Edge headless 自身也因 sandbox 初始化失败崩溃。UA 已确认是 `Edg/157`，复制与诊断代码路径均正确。**在你自己的终端（非沙箱）跑没有这个限制**，Keychain 可正常访问（可能弹一次授权）。若终端里仍失败（例如 Edge 156 的 app-bound encryption），把 `config.toml` 的 `executable_path` 清空，改用 `./run.sh setup` 扫码。

## 需要人工的四件事

| 事项 | 为什么必须人工 | 具体做法 |
|---|---|---|
| 网页端扫码登录一次 | 账号归属 | `./run.sh setup` → 扫 `login-qr.png` |
| 授予 macOS 辅助功能权限 | 读取无障碍树 | CuaDriver（com.trycua.driver）实测已 `auth=2`，无需再授权；仅当改用 agent-desktop 引擎时才需 `tools/node_modules/.bin/agent-desktop permissions --guide` |
| 确认签到按钮在 AX 树可见 | Electron/Shadow DOM 未必暴露 | 授权后 `./run.sh probe`，看 `desktop.checkin_ref` 有没有值；没有就走 CDP |
| （可选）CDP 重启客户端 | 必须带 `--remote-debugging-port` 启动，重启会中断当前对话 | 择时退出后执行 `open -a WorkBuddy --args --remote-debugging-port=9222` |

### 签到后端（优先级）

> 实测更新（2026-11-09）：当前可用后端是 **CuaDriver**（`com.trycua.driver`，TCC `auth=2`）；agent-desktop 仅作为 `engine` 回退，且需要额外授权。

1. **CuaDriver**（当前默认）：`config.toml` 设 `desktop.engine = "cua-driver"`，走 `com.trycua.driver` 的 AX 通道，不移动光标、不抢焦点，已验证可完成真实签到。
2. **CDP**：社区已下架的签到技能就是用 CDP 穿透 Shadow DOM。本项目用 `agent-browser --cdp 9222 eval` 递归遍历 `shadowRoot` 按文字点击。需要客户端带调试端口启动。
3. **computer-use**：会移动物理鼠标，与「不影响人工操作」冲突，`desktop.allow_focus` 默认 `false`，只有显式改 true 才启用。

**必须先验证**：`agent-desktop` 对 Electron + Shadow DOM 内的「立即领取」是否可见。跑 `./run.sh probe`，看 `desktop.snapshot` 里有没有签到按钮的 ref；没有就走 CDP 路线。

## 精确到期与补偿

- 优先读接口 `end_at`（绝对时间）；没有则读 `remaining_seconds` 或页面倒计时 `00:17:55`，用服务端时间校准后算出绝对到期时间戳。
- 到期后延后 `claim_offset_sec`（默认 3 秒）再领，避免服务端尚未结算。
- 领奖成功后立刻再次派遣，并重新读取到期时间，形成闭环。
- 关机 / 睡眠期间错过的到期时间，在下次启动时被夹到 `now`，**立即补领**，不会白跑一趟。
- 每日签到 0 点刷新、当天不领作废；`catch_up=true` 时启动即补签。

## 幂等与安全边界

- 每次领取经 SQLite `(北京时间日期, 类型, 项目)` 唯一键去重，重启、重跑、定时重叠都不会重复领。
- 所有命令输出经脱敏，含 token / cookie / authorization 的行替换为 `<redacted>`；凭据不落盘、不进日志。
- 礼物兑换有 `allow_keywords` / `deny_keywords` 白黑名单，默认屏蔽「去完成 / 充值 / 购买 / 开通 / 升级」等消费类按钮。
- 桌面端命令从不带 `--activate`，网页端从不带 `--headed`（除非你主动改配置）。
- 仅供本人账号、按页面当期规则使用；活动入口、额度、字段会随版本变化，以实际页面为准。

## 许可

本项目代码无第三方依赖。调用的外部 CLI：`agent-browser` 0.27.0（Apache-2.0）、`agent-desktop`（Apache-2.0）。依赖树中不含 BUSL / FSL / PolyForm-NC 等限制性许可。

## 故障排查

| 现象 | 检查 |
|---|---|
| `probe` 显示 desktop 后端 none | 装 agent-desktop 并授权辅助功能，或重启客户端开 9222 |
| 签到按钮找不到 | Shadow DOM 未在 AX 树暴露 → 改 `desktop.engine = "cdp"` |
| 网页端一直停在登录页 | 重跑 `./run.sh setup` 扫码；确认 `web.profile` 目录可写 |
| 到期时间不准 | 在 `config.toml` 填 `web.status_endpoint` 使用结构化 `end_at` |
| 领取没到账 | `./run.sh status` 看运行日志；`daily_checkin.catch_up` 与 `travel.claim_offset_sec` 可调 |

## 实际执行记录（2026-11-09）

真实跑 `./run.sh once` 暴露了 4 个假适配器单测抓不到的问题，已全部修复并补回归测试：

1. **`once` / `daemon` 从不打开网页** → `travel` 只读到 `(no interactive elements)`。修：`Actions.ensure_web_ready()` 在空快照时 `open` + `wait`，并加 `test_reconcile_reopens_empty_web_session`。
2. **`setup` 立刻截图只拿到骨架屏 + spinner**，二维码不可扫。修：`wait_for_login_page()` 轮询直到 AX 快照出现 `Iframe "wechat-qrcode"` 再截图；实测等待后二维码清晰完整。
3. **未登录被误报成 `travel state unknown`**。修：识别登录页特征，明确返回 `login_required` 与 `web session not logged in; run ./run.sh setup first`。
4. **桌面端适配器用了臆造的命令格式**（`click ... --app`、裸 `e7` ref、假定纯文本输出）。修：按 agent-desktop 1.2.0 真实 JSON envelope、全限定 ref `@s1a2b3:e7`、`--session` 一致性重写，并解析 envelope 的 `ok` 而不是只看 returncode；测试 fixture 换成真实 envelope 形状。
5. **`run.sh` 不切换工作目录** → `config.toml` 从未被加载（`login_wait_sec` 与本地 agent-desktop 路径全部失效）。修：run.sh 加 `cd "$DIR"`，`config.load()` 默认路径改为项目根而非 cwd；实测 `status` 现在显示 `桌面端后端: agent-desktop`。
6. **`probe` 不等待重定向** → 把瞬间的 `/app` 误读成已登录。修：`probe()` 在 open 后 `wait(5000)`；实测等待后 URL 仍落回 `/login`，确认未登录。

真实探测结论：

- 网页端未登录跳 `www.workbuddy.cn/login/?platform=agents&state=0&redirect_uri=...`；登录页 AX 快照含 `Iframe "wechat-qrcode" [ref=e11]`，另有企业微信 / 手机号 / SSO / QQ / 邮箱入口。`open` 后立刻读 URL 会短暂显示 `/app`，5 秒后才落到 `/login`，因此所有探测都必须等待渲染稳定。
- `agent-desktop permissions` 实测：`accessibility: denied`、`screen_recording: denied`、`automation: unknown`；`snapshot --app WorkBuddy` 返回 `PERM_DENIED: Accessibility permission not granted`。
- 沙箱内 `osascript` 读 System Events 被直接 SIGTERM，同向印证辅助功能被拦。
- 因此**桌面端签到的唯一阻塞是 macOS 辅助功能授权**，不是代码问题；网页端唯一阻塞是扫码登录。

`login-qr.png` 只在 `setup` 运行窗口内有效（窗口由 `web.login_wait_sec` 控制，当前 600 秒）。**首次 10 分钟窗口已超时、无人扫码，该图已作废**，不要扫旧图；准备好后执行 `./run.sh setup` 重新生成，扫描新图即可。
