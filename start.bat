@echo off
chcp 65001 >nul
title B站自动回复
echo ========================================
echo   B站自动回复机器人启动中...
echo ========================================
echo.

cd /d "%~dp0"

:: 检查是否已有实例在运行
tasklist /FI "WINDOWTITLE eq B站自动回复" 2>NUL | find /I /N "python.exe">NUL
if "%ERRORLEVEL%"=="0" (
    echo [错误] 程序已在运行中，请勿重复启动！
    echo [提示] 如需重启，请先关闭现有窗口
    pause
    exit /b 1
)

:: 检查Python是否安装
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

:: 虚拟环境：优先 .venv，其次 venv
set "VENV_DIR="
if exist ".venv\Scripts\activate.bat" (
    set "VENV_DIR=.venv"
) else if exist "venv\Scripts\activate.bat" (
    set "VENV_DIR=venv"
) else (
    echo [提示] 首次运行，创建虚拟环境 .venv ...
    python -m venv .venv
    set "VENV_DIR=.venv"
)

call "%VENV_DIR%\Scripts\activate.bat"

:: 检查并安装依赖
echo [提示] 检查依赖包...
python -c "import yaml, openai, bilibili_api, fastapi, uvicorn" >nul 2>&1
if %errorlevel% neq 0 (
    echo [提示] 安装依赖包...
    pip install -r requirements.txt
) else (
    echo [信息] 依赖包已安装
)

:: 配置文件
if not exist "config.yaml" (
    if exist "config.example.yaml" (
        echo [提示] 未找到 config.yaml，正在从模板复制...
        copy /Y config.example.yaml config.yaml >nul
        echo [重要] 请编辑 config.yaml，填写 B站 Cookie 与 AI API Key 后再启动
        pause
        exit /b 1
    ) else (
        echo [错误] 缺少 config.yaml 与 config.example.yaml
        pause
        exit /b 1
    )
)

:: 创建必要的目录
if not exist "logs" mkdir logs
if not exist "data" mkdir data

echo [信息] 启动时间: %date% %time%
echo [信息] 按 Ctrl+C 停止程序
echo [信息] Web 控制台默认: http://127.0.0.1:8787/
echo.

python bilibili_auto_reply.py

pause
