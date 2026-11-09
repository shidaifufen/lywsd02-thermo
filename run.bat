@echo off
REM Windows 启动脚本
cd /d %~dp0
if not exist .venv (
  echo 首次运行：创建虚拟环境并安装依赖...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -q -r requirements.txt
echo 打开 http://127.0.0.1:8199/
python server.py
pause
