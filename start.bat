@echo off
setlocal
cd /d "%~dp0"

set "PY=python"
where py >nul 2>nul && set "PY=py -3"

if not exist ".venv" (
  echo [1/3] 创建虚拟环境 .venv ...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo 创建虚拟环境失败，请确认已安装 Python 3.10+
    pause
    exit /b 1
  )
)

call ".venv\Scripts\activate.bat"

echo [2/3] 安装依赖 ...
python -m pip install -q --upgrade pip
python -m pip install -q -e ".[server]"
if errorlevel 1 (
  echo 依赖安装失败
  pause
  exit /b 1
)

echo [3/3] 启动服务：http://127.0.0.1:8010
echo       （Ctrl+C 停止）
python -m server.main

pause
