#!/bin/zsh
# 让 WorkBuddy 客户端暴露 CDP 调试端口（供 agent-browser / 本技能连接）。
#
# 关键事实（2026-11-14 读 app.asar + 两台 5.11.3 实测）：
#   - 客户端启动时只读一次 WORKBUDDY_REMOTE_DEBUGGING_PORT（正则 /^\d+$/），
#     然后 appendSwitch('remote-debugging-port', n)；
#     命令行参数 --remote-debugging-port 会被忽略，别再用 open --args 那套。
#   - 端口被占时**没有任何回退**，Chromium 直接起不来调试服务。
#   - 手动 launchctl setenv 只活在当次登录会话，重启电脑就丢；
#     持久化靠 com.local.workbuddy-cdp-env 这个 LaunchAgent。
#
# 用法：./scripts/launch-workbuddy-debug.sh [port]
#   不给端口时依次取：客户端自报的 DevToolsActivePort → 9222
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="WorkBuddy"
PORT="${1:-}"
PORT_FILE="$HOME/.workbuddy/app/session/DevToolsActivePort"

cdp_ok() { curl -s -m 2 "http://127.0.0.1:$1/json/version" 2>/dev/null | grep -q '"Browser"'; }

# 0) 版本红线：5.9.x 及更早的成长中心是网页版活动中心，客户端里没有内嵌成长中心
#    和「Buddy加油站」浮层 —— 端口打开了也跑不起来，先升级再说。
APP_BUNDLE="${WORKBUDDY_APP:-/Applications/WorkBuddy.app}"
[ -d "$APP_BUNDLE" ] || APP_BUNDLE="$HOME/Applications/WorkBuddy.app"
CW_VERSION=""
if [ -d "$APP_BUNDLE" ]; then
  CW_VERSION="$(defaults read "$APP_BUNDLE/Contents/Info.plist" CFBundleShortVersionString 2>/dev/null)"
fi
if [ -n "$CW_VERSION" ]; then
  CW_MAJOR="${CW_VERSION%%.*}"
  CW_REST="${CW_VERSION#*.}"
  CW_MINOR="${CW_REST%%.*}"
  case "$CW_MAJOR$CW_MINOR" in
    (*[!0-9]*|"") CW_MAJOR=0; CW_MINOR=0 ;;
  esac
  if [ "$CW_MAJOR" -lt 5 ] || { [ "$CW_MAJOR" -eq 5 ] && [ "$CW_MINOR" -lt 11 ]; }; then
    echo "✗ WorkBuddy $CW_VERSION 太旧：5.9.x 及更早的成长中心是网页版活动中心，"
    echo "  客户端里没有内嵌成长中心和「Buddy加油站」浮层，本技能无法工作。需要 5.11.0+。"
    echo ""
    echo "升级方式一（官方）：左下角头像 → 检查更新（5.11 目前灰度，可能提示已是最新）"
    echo "升级方式二：官方安装包覆盖安装"
    echo "  Apple 芯片：https://download.codebuddy.cn/workbuddy/saas/darwin-arm64/WorkBuddy-darwin-arm64-5.11.3.43806934-ecace0f7.dmg"
    echo "  Intel 芯片：https://download.codebuddy.cn/workbuddy/saas/darwin-x64/WorkBuddy-darwin-x64-5.11.3.43806934-ecace0f7.dmg"
    echo "  下载后拖进「应用程序」，退出旧版再启动，然后重跑本脚本。"
    echo "  注意：官网「历史版本下载」页最新只到 5.9.3，那个版本装了也跑不起来。"
    exit 4
  fi
  echo "WorkBuddy $CW_VERSION（要求 ≥5.11.0）"
fi

if [ -z "$PORT" ] && [ -f "$PORT_FILE" ]; then
  PORT="$(head -1 "$PORT_FILE" | tr -d '[:space:]')"
fi
PORT="${PORT:-9222}"

if cdp_ok "$PORT"; then
  echo "OK: CDP already listening on port $PORT"
  exit 0
fi

# 1) 写端口：setenv 立即生效 + plist 保证重启后仍在 + 同步 config.toml
echo "Pinning CDP port $PORT ..."
"$ROOT/run.sh" ports negotiate --apply --port "$PORT" || {
  echo "FAIL: 端口写入失败（多半是 $PORT 已被别的程序占用）。先看："
  echo "  $ROOT/run.sh ports status"
  exit 3
}

# 2) 优雅退出客户端（保存会话，不强杀）
echo "Quitting $APP gracefully..."
osascript -e "tell application \"$APP\" to quit" 2>/dev/null || true
for i in {1..20}; do
  sleep 1
  pgrep -x "$APP" >/dev/null 2>&1 || break
  if [ "$i" -eq 20 ]; then
    echo "WARN: $APP still running after 20s; aborting to avoid force-kill."
    exit 1
  fi
done
sleep 2

# 3) 正常拉起（端口由环境变量带入，不需要也不该带命令行参数）
echo "Launching $APP ..."
open -a "$APP"

# 4) 探测 CDP（最长 30s）
for i in {1..30}; do
  sleep 1
  if cdp_ok "$PORT"; then
    echo "OK: CDP enabled on port $PORT"
    echo "--- targets ---"
    curl -s -m 3 "http://127.0.0.1:$PORT/json/list" | python3 -c "
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
done

echo "FAIL: 客户端已启动但 $PORT 上没有 CDP。排查："
echo "  launchctl print gui/\$(id -u) | grep -i WORKBUDDY   # 环境变量是否生效"
echo "  $ROOT/run.sh ports status                          # 端口全貌与归属"
exit 2
