#!/usr/bin/env bash
# 初始化运行环境：创建项目自带的 venv 并安装依赖。
# 可用 PYTHON 环境变量指定基础解释器（需 Python 3.10+），例如：
#   PYTHON=/usr/local/bin/python3 ./scripts/setup.sh
set -euo pipefail

cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"

echo "==> 使用基础解释器: $("$PY" -c 'import sys; print(sys.executable)')"
"$PY" -m venv .venv
.venv/bin/pip install --upgrade -r requirements.txt
echo "==> 完成。启动: ./scripts/start.sh"
