#!/bin/bash
cd "$(dirname "$0")" || exit 1
echo "=========================================="
echo "  温湿度计看板 - 首次运行会自动装依赖"
echo "=========================================="
PY=""
for c in python3.12 python3.11 python3.10 python3; do
  command -v "$c" >/dev/null 2>&1 && PY="$c" && break
done
if [ -z "$PY" ]; then
  echo "[X] 没有找到 Python 3，请先安装：https://www.python.org/downloads/macos/"
  open "https://www.python.org/downloads/macos/" 2>/dev/null
  read -n 1 -s -r -p "按任意键退出…"; exit 1
fi
[ -d .venv ] || { echo "[1/3] 创建独立运行环境…"; "$PY" -m venv .venv || exit 1; }
. .venv/bin/activate
echo "[2/3] 检查依赖…"
if ! python -c "import aiohttp, bleak" >/dev/null 2>&1; then
  echo "      正在安装 aiohttp / bleak（需要联网，约 1 分钟）…"
  python -m pip install -q --upgrade pip
  python -m pip install -q -r requirements.txt || { echo "[X] 依赖安装失败"; read -n 1 -s -r; exit 1; }
fi
echo "[3/3] 启动服务，浏览器会自动打开…"
( sleep 2; open "http://127.0.0.1:8199/" ) &
python server.py
