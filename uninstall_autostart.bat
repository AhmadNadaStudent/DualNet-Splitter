@echo off
setlocal
set "DUALNET_LAUNCHER=%~f0"
cd /d "%~dp0"
title DualNet Splitter - Hapus Autostart Windows

:: Check for Administrator privileges
net session >nul 2>&1
if errorlevel 1 (
    echo [INFO] Memerlukan izin Administrator untuk menghapus Task Scheduler...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath $env:DUALNET_LAUNCHER -Verb RunAs"
    exit /b
)

echo =========================================================================
echo       PENGHAPUSAN AUTOSTART DUALNET VIRTUAL ADAPTER (TUN MODE)
echo =========================================================================
echo.
echo Menghapus DualNetTUNAutoStart dari Windows Task Scheduler...
echo.

where py >nul 2>&1
if %errorLevel% == 0 (
    py -c "from engine.autostart import uninstall_autostart; success, msg = uninstall_autostart(); print(msg); input('Tekan Enter untuk selesai...')"
    exit /b
)

where python >nul 2>&1
if %errorLevel% == 0 (
    python -c "from engine.autostart import uninstall_autostart; success, msg = uninstall_autostart(); print(msg); input('Tekan Enter untuk selesai...')"
    exit /b
)

echo [ERROR] Python tidak ditemukan di sistem!
pause
