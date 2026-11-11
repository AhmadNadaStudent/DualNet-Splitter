@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if not errorlevel 1 (
    py tools\launch_steam.py
) else (
    python tools\launch_steam.py
)
pause
