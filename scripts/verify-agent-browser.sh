#!/bin/zsh
# CDP 链路验证：连上 WorkBuddy 客户端 → 列出 targets → 读成长中心状态
# 前置：客户端已带 WORKBUDDY_REMOTE_DEBUGGING_PORT 启动（./run.sh ports status 可确认）
# 用法：./scripts/verify-agent-browser.sh [port]
#   不给端口时取客户端自报的 DevToolsActivePort（跟随客户端实际端口）

set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${1:-}"
if [ -z "$PORT" ]; then
  PF="$HOME/.workbuddy/app/session/DevToolsActivePort"
  [ -f "$PF" ] && PORT="$(head -1 "$PF" | tr -d '[:space:]')"
fi
PORT="${PORT:-9222}"
AB="$ROOT/tools/node_modules/.bin/agent-browser"

step() { echo "\n===== $* ====="; }

step "0. 版本与 CDP 探测"
if [ ! -x "$AB" ]; then
  echo "FAIL: agent-browser 未安装，先执行：cd $ROOT/tools && npm install"
  exit 1
fi
"$AB" --version 2>&1 | tail -1
if ! curl -s -m 2 "http://127.0.0.1:$PORT/json/version" | grep -q "Browser"; then
  echo "FAIL: CDP 未就绪（port $PORT）。先看端口全貌：$ROOT/run.sh ports status"
  echo "  需要重挂端口并重启客户端：$ROOT/scripts/launch-workbuddy-debug.sh $PORT"
  exit 2
fi
echo "OK: CDP alive"

step "1. targets（成长中心打开时会出现 type=iframe）"
curl -s "http://127.0.0.1:$PORT/json/list" | python3 -c "
import json,sys
ts=json.load(sys.stdin)
print(f'{len(ts)} targets:')
for t in ts:
    print(f\"  [{t.get('type','?'):<7}] {(t.get('title') or '')[:34]:<36} {(t.get('url') or '')[:60]}\")
"

step "2. 清理旧 daemon（单例，切版本必须 close --all）"
"$AB" close --all 2>&1 | tail -2 || true
sleep 1

step "3. 连接"
"$AB" connect "$PORT" 2>&1 | tail -2

step "4. 技能自带只读诊断（推荐，信息最全）"
cd "$ROOT" && ./run.sh inspect 2>&1 | head -40
