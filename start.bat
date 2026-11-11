@echo off
setlocal
set "DUALNET_LAUNCHER=%~f0"
cd /d "%~dp0"

:: If standalone EXE exists, launch it directly (EXE already embeds UAC Admin manifest)
if exist "DualNet.exe" (
    start "" "DualNet.exe"
    exit /b
)

:: Check for Administrator privileges for Python fallback
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath $env:DUALNET_LAUNCHER -Verb RunAs"
    exit /b
)

where.exe pyw >nul 2>&1
if %errorLevel% == 0 (
    start "" pyw gui.py
    exit /b
)

where.exe pythonw >nul 2>&1
if %errorLevel% == 0 (
    start "" pythonw gui.py
    exit /b
)

where.exe py >nul 2>&1
if %errorLevel% == 0 (
    start "" py gui.py
    exit /b
)

where.exe python >nul 2>&1
if %errorLevel% == 0 (
    start "" python gui.py
    exit /b
)

pause
