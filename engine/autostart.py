"""
DualNet Splitter - Windows Autostart Management Module.
Manages Windows Scheduled Task to run DualNet in the tray with Highest Privileges.
"""

import os
import sys
import subprocess
import shutil
from typing import Tuple, Optional

TASK_NAME = "DualNetTUNAutoStart"
from engine.winutil import APP_DIR as BASE_DIR, IS_FROZEN
RUNNER_SCRIPT = os.path.join(BASE_DIR, "gui.py")

def get_pythonw_executable() -> str:
    """Finds pythonw.exe (windowless python) or falls back to python.exe."""
    # Check current python dir
    current_dir = os.path.dirname(sys.executable)
    pyw = os.path.join(current_dir, "pythonw.exe")
    if os.path.exists(pyw):
        return pyw

    # Fallback to PATH search
    which_pyw = shutil.which("pythonw")
    if which_pyw:
        return which_pyw

    which_py = shutil.which("python")
    if which_py:
        return which_py

    return sys.executable

def is_autostart_installed() -> bool:
    """Checks if the DualNetTUNAutoStart task is registered in Windows Task Scheduler."""
    try:
        res = subprocess.run(
            ["schtasks", "/query", "/tn", TASK_NAME],
            capture_output=True,
            text=True,
            timeout=5
        )
        return res.returncode == 0
    except Exception:
        return False

def install_autostart() -> Tuple[bool, str]:
    """Registers the scheduled task using PowerShell Register-ScheduledTask with Highest privileges."""
    if IS_FROZEN:
        # Packaged build: start the GUI hidden, with its permanent tray icon.
        py_exec = sys.executable
        task_args = "--autostart"
    else:
        py_exec = get_pythonw_executable()
        if not os.path.exists(RUNNER_SCRIPT):
            return False, f"Script runner tidak ditemukan: {RUNNER_SCRIPT}"
        task_args = f'"{RUNNER_SCRIPT}" --autostart'

    # PowerShell single-quoted literals escape apostrophes by doubling them.
    py_exec = py_exec.replace("'", "''")
    task_args = task_args.replace("'", "''")
    base_dir = BASE_DIR.replace("'", "''")
    ps_script = f"""
    $ErrorActionPreference = 'Stop'
    $action = New-ScheduledTaskAction -Execute '{py_exec}' -Argument '{task_args}' -WorkingDirectory '{base_dir}'
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\\$env:USERNAME" -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 0) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force
    """

    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=15
        )
        if proc.returncode == 0:
            return True, f"Berhasil mendaftarkan Autostart Windows ({TASK_NAME}) dengan hak Administrator penuh."
        else:
            err = proc.stderr.strip() or proc.stdout.strip()
            return False, f"Gagal mendaftarkan task: {err}"
    except Exception as e:
        return False, f"Exception saat registrasi task: {e}"

def uninstall_autostart() -> Tuple[bool, str]:
    """Removes the scheduled task from Windows Task Scheduler."""
    try:
        proc = subprocess.run(
            ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if proc.returncode == 0:
            return True, f"Autostart Windows ({TASK_NAME}) telah dihapus."
        else:
            return False, f"Gagal menghapus autostart: {proc.stderr.strip()}"
    except Exception as e:
        return False, f"Exception saat menghapus task: {e}"
