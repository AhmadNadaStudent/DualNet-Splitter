@echo off
setlocal
set "DUALNET_LAUNCHER=%~f0"
cd /d "%~dp0"
title DualNet Splitter Terminal Dashboard

net session >nul 2>&1
if errorlevel 1 (
    echo [INFO] Memerlukan izin Administrator...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath $env:DUALNET_LAUNCHER -Verb RunAs"
    exit /b
)

where py >nul 2>&1
if %errorLevel% == 0 (
    py main.py
    exit /b
)

where python >nul 2>&1
if %errorLevel% == 0 (
    python main.py
    exit /b
)

echo [ERROR] Python tidak ditemukan!
pause
