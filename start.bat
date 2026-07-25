@echo off
chcp 65001 >nul
title B站自动回复
echo ========================================
echo   B站自动回复机器人启动中...
echo ========================================
echo.

cd /d "%~dp0"

:: 检查是否已有实例在运行
tasklist /FI "WINDOWTITLE eq B站自动回复机器人" 2>NUL | find /I /N "python.exe">NUL
if "%ERRORLEVEL%"=="0" (
    echo [错误] 程序已在运行中，请勿重复启动！
    echo [提示] 如需重启，请先关闭现有窗口
    pause
    exit /b 1
)

:: 检查Python是否安装
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到Python，请先安装Python
    pause
    exit /b 1
)

:: 检查虚拟环境
if not exist "venv" (
    echo [提示] 首次运行，创建虚拟环境...
    python -m venv venv
)

call venv\Scripts\activate.bat

:: 检查并安装依赖
echo [提示] 检查依赖包...
pip show pyyaml >nul 2>&1
if %errorlevel% neq 0 (
    echo [提示] 安装依赖包...
    pip install -r requirements.txt
) else (
    echo [信息] 依赖包已安装
)

:: 创建必要的目录
if not exist "logs" mkdir logs
if not exist "data" mkdir data

echo [信息] 启动时间: %date% %time%
echo [信息] 按 Ctrl+C 停止程序
echo.

python bilibili_auto_reply.py

pause
