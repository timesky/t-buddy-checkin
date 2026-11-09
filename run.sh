#!/bin/sh
# 独立 CLI 入口：把 src 挂到 PYTHONPATH，零 pip 安装即可运行。
set -e
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$DIR"
export PYTHONPATH="$DIR/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m buddy_checkin "$@"
