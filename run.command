#!/bin/bash
# macOS / Linux 启动脚本
cd "$(dirname "$0")" || exit 1
if [ ! -d .venv ]; then
  echo "首次运行：创建虚拟环境并安装依赖…"
  python3 -m venv .venv || exit 1
fi
. .venv/bin/activate
python -m pip install -q -r requirements.txt || exit 1
echo "打开 http://127.0.0.1:8199/"
python server.py
