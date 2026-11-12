#!/bin/zsh
# CDP 链路验证：连上 WorkBuddy 客户端 → 列出 targets → 读成长中心状态
# 前置：launchctl setenv WORKBUDDY_REMOTE_DEBUGGING_PORT 9222 且客户端已重启
# 用法：./scripts/verify-agent-browser.sh [port]

set -u
PORT="${1:-9222}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AB="$ROOT/tools/node_modules/.bin/agent-browser"

step() { echo "\n===== $* ====="; }

step "0. 版本与 CDP 探测"
if [ ! -x "$AB" ]; then
  echo "FAIL: agent-browser 未安装，先执行：cd $ROOT/tools && npm install"
  exit 1
fi
"$AB" --version 2>&1 | tail -1
if ! curl -s -m 2 "http://127.0.0.1:$PORT/json/version" | grep -q "Browser"; then
  echo "FAIL: CDP 未就绪（port $PORT）。先执行："
  echo "  launchctl setenv WORKBUDDY_REMOTE_DEBUGGING_PORT $PORT"
  echo "  然后重启客户端，或跑 $ROOT/scripts/launch-workbuddy-debug.sh"
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
