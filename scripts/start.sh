#!/usr/bin/env bash
# 启动网关（默认前台运行，Ctrl+C 退出并回收全部子项目进程）。
#   ./scripts/start.sh            前台运行
#   ./scripts/start.sh -d         后台守护运行（日志: logs/gateway.log）
#   WC_PORT=39000 ./scripts/start.sh   临时换端口
set -euo pipefail

cd "$(dirname "$0")/.."
PIDFILE="logs/gateway.pid"

# 优先使用项目 venv；否则回退到任何装有 aiohttp 的 python3
if [[ -x .venv/bin/python ]]; then
    PY=".venv/bin/python"
else
    PY="$(command -v python3 || true)"
    if [[ -z "$PY" ]] || ! "$PY" -c 'import aiohttp' 2>/dev/null; then
        echo "错误: 未找到可用的 Python 环境（需要 aiohttp）。请先执行 ./scripts/setup.sh" >&2
        exit 1
    fi
fi

mkdir -p logs

if [[ "${1:-}" == "-d" ]]; then
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "网关已在运行 (pid $(cat "$PIDFILE"))；如需重启请先 ./scripts/stop.sh" >&2
        exit 1
    fi
    nohup "$PY" -m gateway.server >> logs/gateway.log 2>&1 &
    echo $! > "$PIDFILE"
    echo "网关已后台启动 (pid $(cat "$PIDFILE"))，端口 ${WC_PORT:-38000}"
    echo "日志: logs/gateway.log；停止: ./scripts/stop.sh"
else
    exec "$PY" -m gateway.server
fi
