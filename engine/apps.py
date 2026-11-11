"""Discover optional desktop applications without machine-specific paths."""
import os
import shutil


def find_steam():
    candidates = []
    try:
        import winreg
        for root, key, name in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamExe"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(root, key) as handle:
                    value = winreg.QueryValueEx(handle, name)[0]
                candidates.append(value if name == "SteamExe" else os.path.join(value, "steam.exe"))
            except OSError:
                pass
    except ImportError:
        pass
    candidates.append(shutil.which("steam.exe"))
    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        if os.environ.get(variable):
            candidates.append(os.path.join(os.environ[variable], "Steam", "steam.exe"))
    return next((path for path in candidates if path and os.path.isfile(path)), None)
