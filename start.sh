#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

if [ ! -d ".venv" ]; then
  echo "[1/3] 创建虚拟环境 .venv ..."
  "$PY" -m venv .venv
fi

# shellcheck disable=SC1091
. .venv/bin/activate

echo "[2/3] 安装依赖 ..."
pip install -q --upgrade pip
pip install -q -e ".[server]"

echo "[3/3] 启动服务：http://127.0.0.1:8010"
echo "      （Ctrl+C 停止）"
exec python -m server.main
