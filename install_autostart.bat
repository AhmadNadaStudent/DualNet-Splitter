@echo off
setlocal
set "DUALNET_LAUNCHER=%~f0"
cd /d "%~dp0"
title DualNet Splitter - Pasang Autostart Windows

:: Check for Administrator privileges
net session >nul 2>&1
if errorlevel 1 (
    echo [INFO] Memerlukan izin Administrator untuk mendaftarkan Task Scheduler...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath $env:DUALNET_LAUNCHER -Verb RunAs"
    exit /b
)

echo =========================================================================
echo       PEMASANGAN AUTOSTART DUALNET VIRTUAL ADAPTER (TUN MODE)
echo =========================================================================
echo.
echo Mendaftarkan DualNetTUNAutoStart ke Windows Task Scheduler...
echo Mode ini akan berjalan otomatis secara hening di background setiap kali
echo komputer dinyalakan / login, tanpa jendela hitam (CMD) yang mengganggu.
echo.

where py >nul 2>&1
if %errorLevel% == 0 (
    py -c "from engine.autostart import install_autostart; success, msg = install_autostart(); print(msg); input('Tekan Enter untuk selesai...')"
    exit /b
)

where python >nul 2>&1
if %errorLevel% == 0 (
    python -c "from engine.autostart import install_autostart; success, msg = install_autostart(); print(msg); input('Tekan Enter untuk selesai...')"
    exit /b
)

echo [ERROR] Python tidak ditemukan di sistem!
pause
