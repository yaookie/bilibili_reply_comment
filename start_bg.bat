@echo off
chcp 65001 >nul
cd /d "%~dp0"

:: 以最小化窗口启动，便于用浏览器看控制台，而不占用前台
echo 正在最小化启动 B站自动回复...
echo 控制台地址: http://127.0.0.1:8787/
echo.
start "B站自动回复" /min cmd /c "call start.bat"

timeout /t 2 >nul
start "" "http://127.0.0.1:8787/"
echo 已启动。可用任务栏中的最小化窗口结束进程（关闭该窗口）。
pause
