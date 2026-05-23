@echo off
chcp 65001 >nul
title ZeroSync - Studio Guard Launcher
setlocal enabledelayedexpansion

echo [ZeroSync] 檢查環境組件中...

:: 自動安裝最新 AI 核心插件 與 加密插件 (cryptography)
py -m pip install PySide6 GitPython requests google-genai openai cryptography --quiet --no-warn-script-location

if %errorlevel% neq 0 (
    echo [Error] 無法安裝必要組件，請確認 Python 已安裝並已加入環境變數。
    pause
    exit
)

:: 啟動位於子資料夾的核心程式
if exist "ZeroSync_App\main.py" (
    echo [ZeroSync] 核心啟動中...
    py ZeroSync_App\main.py
) else (
    echo [Error] 找不到 ZeroSync_App\main.py，請確認檔案結構完整性。
    pause
    exit
)