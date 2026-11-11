@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if not errorlevel 1 (
    py tools\set_metric.py reset
) else (
    python tools\set_metric.py reset
)
pause
