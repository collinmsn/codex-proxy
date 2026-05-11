@echo off
title Codex Proxy - DeepSeek
color 0A

echo ========================================
echo    Codex Proxy - DeepSeek
echo ========================================
echo.

REM 请在此处填入你的 DeepSeek API Key
set DEEPSEEK_API_KEY=sk-xxxxxxxxxxxx

REM 检查 API Key 是否已设置
if "%DEEPSEEK_API_KEY%"=="sk-xxxxxxxxxxxx" (
    echo [错误] 请先设置 DEEPSEEK_API_KEY！
    echo.
    echo 获取地址：https://platform.deepseek.com
    echo.
    pause
    exit /b 1
)

echo [信息] 启动代理服务器...
echo [信息] 上游 API: https://api.deepseek.com
echo [信息] 监听端口: 9090
echo.
echo 启动成功后，请打开 Codex 桌面端。
echo 按 Ctrl+C 可停止代理。
echo ========================================
echo.

python "%~dp0codex_proxy.py" --upstream https://api.deepseek.com --port 9090

pause
