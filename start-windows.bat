@echo off
chcp 65001 >nul
title 温湿度计看板
cd /d %~dp0
echo ==========================================
echo   温湿度计看板 - 首次运行会自动装依赖
echo ==========================================
echo.
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY ( where python >nul 2>nul && set PY=python )
if not defined PY (
  echo [X] 没有找到 Python。请先安装 Python 3.10+，安装时勾选 "Add Python to PATH"。
  start https://www.python.org/downloads/
  pause
  exit /b 1
)
if not exist .venv (
  echo [1/3] 创建独立运行环境（只做一次）...
  %PY% -m venv .venv || goto :fail
)
call .venv\Scripts\activate.bat
echo [2/3] 检查依赖...
python -c "import aiohttp, bleak" >nul 2>nul
if errorlevel 1 (
  echo      正在安装 aiohttp / bleak（需要联网，约 1 分钟）...
  python -m pip install -q --upgrade pip
  python -m pip install -q -r requirements.txt || goto :fail
)
echo [3/3] 启动服务，浏览器会自动打开...
start "" http://127.0.0.1:8199/
python server.py
goto :eof
:fail
echo.
echo [X] 启动失败，把上面的报错发到 GitHub Issues。
pause
