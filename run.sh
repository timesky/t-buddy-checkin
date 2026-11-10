#!/bin/sh
# 独立 CLI 入口：把 src 挂到 PYTHONPATH，零 pip 安装即可运行。
# 需要 Python >= 3.11（tomllib）。launchd 等最小 PATH 环境下 python3 可能解析到
# 系统 3.9，所以这里显式挑选满足版本要求的解释器（可用 BUDDY_PYTHON 覆盖）。
set -e
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$DIR"
export PYTHONPATH="$DIR/src${PYTHONPATH:+:$PYTHONPATH}"
# launchd 把 stdout 重定向到文件时 Python 默认块缓冲，日志会长期为空
export PYTHONUNBUFFERED=1

PY=""
if [ -n "$BUDDY_PYTHON" ]; then
    PY="$BUDDY_PYTHON"
else
    for cand in \
        /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
        /opt/homebrew/bin/python3 \
        /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/local/bin/python3.11 \
        /usr/local/bin/python3 \
        python3.13 python3.12 python3.11 python3; do
        if command -v "$cand" >/dev/null 2>&1; then
            if "$cand" -c 'import sys, tomllib; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
                PY="$cand"
                break
            fi
        fi
    done
fi

if [ -z "$PY" ]; then
    echo "run.sh: 找不到 Python >= 3.11（需要 tomllib）。请安装后重试，或用 BUDDY_PYTHON 指定路径。" >&2
    exit 1
fi

exec "$PY" -m buddy_checkin "$@"
