"""DualNet Splitter engine package."""
from engine.winutil import hide_subprocess_windows

# Every helper subprocess (powershell, schtasks, sing-box) must stay invisible.
hide_subprocess_windows()
