#!/bin/zsh
# 用 --remote-debugging-port 重启 WorkBuddy 客户端，供 agent-browser (CDP) 连接。
# 用途：browser 引擎在客户端内嵌 webview 上 DOM 级操作签到/领礼物/派遣/兑换/抽奖。
# 安全：优雅退出（osascript，保存会话）；若带参数启动失败，自动不带参数拉起客户端兜底。

set -u
APP="WorkBuddy"
PORT="${1:-9222}"
CDP_URL="http://127.0.0.1:$PORT/json/version"
LIST_URL="http://127.0.0.1:$PORT/json/list"

cdp_ok() { curl -s -m 2 "$CDP_URL" 2>/dev/null | grep -q '"webSocketDebuggerUrl"\|"Browser"'; }

# 0) 设置 WorkBuddy 官方 CDP 环境变量（主进程读 WORKBUDDY_REMOTE_DEBUGGING_PORT）
#    在 iTerm/Terminal 里运行本脚本时有权限；从受限上下文调用失败则提示手动执行。
launchctl setenv WORKBUDDY_REMOTE_DEBUGGING_PORT "$PORT" 2>/dev/null || {
  echo "NOTE: launchctl setenv 被拒绝。请在 iTerm 里先手动执行一次："
  echo "      launchctl setenv WORKBUDDY_REMOTE_DEBUGGING_PORT $PORT"
  [ "$(launchctl getenv WORKBUDDY_REMOTE_DEBUGGING_PORT 2>/dev/null)" = "$PORT" ] || exit 3
}

# 1) 已带调试端口运行 → 直接成功
if cdp_ok; then
  echo "OK: CDP already listening on port $PORT"
  exit 0
fi

# 2) 优雅退出客户端
echo "Quitting $APP gracefully..."
osascript -e "tell application \"$APP\" to quit" 2>/dev/null || true
for i in {1..15}; do
  sleep 1
  pgrep -x "$APP" >/dev/null 2>&1 || break
  if [ "$i" -eq 15 ]; then
    echo "WARN: $APP still running after 15s; aborting to avoid force-kill."
    exit 1
  fi
done
sleep 2

# 3) 带调试端口重启
echo "Launching $APP with --remote-debugging-port=$PORT ..."
open -a "$APP" --args --remote-debugging-port="$PORT"

# 4) 探测 CDP（最长 30s）
ok=0
for i in {1..30}; do
  sleep 1
  if cdp_ok; then ok=1; break; fi
done

if [ "$ok" -eq 1 ]; then
  echo "OK: CDP enabled on port $PORT"
  echo "--- targets ---"
  curl -s -m 3 "$LIST_URL" | python3 -c "
import json,sys
try:
    ts=json.load(sys.stdin)
    for t in ts:
        print(f\"  [{t.get('type','?')}] {t.get('title','')[:50]}  {t.get('url','')[:80]}\")
except Exception as e:
    print('  (target list parse failed:', e, ')')
"
  exit 0
fi

# 5) 兜底：不带参数拉起客户端，保证 App 恢复运行
echo "FAIL: CDP did not come up. Relaunching $APP WITHOUT debug flag (fallback)..."
open -a "$APP"
exit 2
