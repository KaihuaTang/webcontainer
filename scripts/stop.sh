#!/usr/bin/env bash
# 停止后台运行的网关；网关退出时会一并回收全部子项目进程。
set -euo pipefail

cd "$(dirname "$0")/.."
PIDFILE="logs/gateway.pid"

if [[ ! -f "$PIDFILE" ]]; then
    echo "未找到 $PIDFILE，网关可能不是通过 start.sh -d 启动的。" >&2
    exit 1
fi

PID="$(cat "$PIDFILE")"
if ! kill -0 "$PID" 2>/dev/null; then
    echo "进程 $PID 已不存在，清理 pid 文件。"
    rm -f "$PIDFILE"
    exit 0
fi

echo "停止网关 (pid $PID)…"
kill "$PID"
for _ in $(seq 1 20); do
    kill -0 "$PID" 2>/dev/null || { rm -f "$PIDFILE"; echo "已停止。"; exit 0; }
    sleep 0.5
done
echo "等待超时，强制结束。"
kill -9 "$PID" 2>/dev/null || true
rm -f "$PIDFILE"
