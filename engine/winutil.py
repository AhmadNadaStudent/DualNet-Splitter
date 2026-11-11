"""
Windows helper utilities for DualNet Splitter.
- APP_DIR: base folder that works for both source (.py) and frozen (.exe) builds.
- hide_subprocess_windows(): forces every subprocess to spawn without a console window
  (prevents the black terminal flashing when running from a windowed .exe / pythonw).
- icmp_ping(): silent ping through a specific source IP using the native Windows ICMP API
  (IcmpSendEcho2Ex) - no ping.exe process is spawned at all.
- process_exists(): checks running processes via Toolhelp32 snapshot - no tasklist.exe.
"""

import os
import sys
import ctypes
import socket
import struct
import subprocess
from ctypes import wintypes
from typing import Optional

# ---------------------------------------------------------------------------
# Application base directory (source checkout or folder that contains the .exe)
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

IS_FROZEN = bool(getattr(sys, "frozen", False))

CREATE_NO_WINDOW = 0x08000000

# ---------------------------------------------------------------------------
# Hide console windows of every child process
# ---------------------------------------------------------------------------
_patched = False


def hide_subprocess_windows():
    """Monkey-patches subprocess.Popen so every child runs with CREATE_NO_WINDOW."""
    global _patched
    if _patched or os.name != "nt":
        return
    _orig_init = subprocess.Popen.__init__

    def _init(self, *args, **kwargs):
        # CREATE_NO_WINDOW only affects console programs (powershell, schtasks, sing-box);
        # GUI apps opened via os.startfile are unaffected.
        flags = kwargs.get("creationflags", 0) or 0
        kwargs["creationflags"] = flags | CREATE_NO_WINDOW
        _orig_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _init
    _patched = True


# ---------------------------------------------------------------------------
# Native silent ICMP ping
# ---------------------------------------------------------------------------
class _IP_OPTION_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Ttl", ctypes.c_ubyte),
        ("Tos", ctypes.c_ubyte),
        ("Flags", ctypes.c_ubyte),
        ("OptionsSize", ctypes.c_ubyte),
        ("OptionsData", ctypes.c_void_p),
    ]


class _ICMP_ECHO_REPLY(ctypes.Structure):
    _fields_ = [
        ("Address", wintypes.ULONG),
        ("Status", wintypes.ULONG),
        ("RoundTripTime", wintypes.ULONG),
        ("DataSize", wintypes.USHORT),
        ("Reserved", wintypes.USHORT),
        ("Data", ctypes.c_void_p),
        ("Options", _IP_OPTION_INFORMATION),
    ]


_icmp = None
if os.name == "nt":
    try:
        _icmp = ctypes.WinDLL("iphlpapi.dll")
        _icmp.IcmpCreateFile.restype = wintypes.HANDLE
        _icmp.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
        _icmp.IcmpSendEcho2Ex.restype = wintypes.DWORD
        _icmp.IcmpSendEcho2Ex.argtypes = [
            wintypes.HANDLE,   # IcmpHandle
            wintypes.HANDLE,   # Event
            ctypes.c_void_p,   # ApcRoutine
            ctypes.c_void_p,   # ApcContext
            wintypes.ULONG,    # SourceAddress
            wintypes.ULONG,    # DestinationAddress
            ctypes.c_void_p,   # RequestData
            wintypes.WORD,     # RequestSize
            ctypes.c_void_p,   # RequestOptions
            ctypes.c_void_p,   # ReplyBuffer
            wintypes.DWORD,    # ReplySize
            wintypes.DWORD,    # Timeout
        ]
    except Exception:
        _icmp = None


def _ip_to_ulong(ip: str) -> int:
    return struct.unpack("<L", socket.inet_aton(ip))[0]


def icmp_ping(source_ip: Optional[str], target: str = "1.1.1.1", timeout_ms: int = 1000) -> Optional[float]:
    """Sends one ICMP echo from source_ip to target. Returns RTT in ms or None."""
    if _icmp is None or not source_ip or source_ip.startswith("169.254"):
        return None
    handle = _icmp.IcmpCreateFile()
    if not handle or handle == wintypes.HANDLE(-1).value:
        return None
    try:
        payload = b"DualNetPing"
        req = ctypes.create_string_buffer(payload, len(payload))
        reply_size = ctypes.sizeof(_ICMP_ECHO_REPLY) + len(payload) + 8 + 64
        reply = ctypes.create_string_buffer(reply_size)
        n = _icmp.IcmpSendEcho2Ex(
            handle, None, None, None,
            _ip_to_ulong(source_ip), _ip_to_ulong(target),
            ctypes.cast(req, ctypes.c_void_p), len(payload), None,
            ctypes.cast(reply, ctypes.c_void_p), reply_size, timeout_ms,
        )
        if n == 0:
            return None
        echo = _ICMP_ECHO_REPLY.from_buffer_copy(reply)
        if echo.Status != 0:  # IP_SUCCESS
            return None
        return float(max(echo.RoundTripTime, 0.5) if echo.RoundTripTime else 0.5)
    except Exception:
        return None
    finally:
        _icmp.IcmpCloseHandle(handle)


# ---------------------------------------------------------------------------
# Process lookup without spawning tasklist.exe
# ---------------------------------------------------------------------------
class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def process_exists(exe_name: str) -> bool:
    """Returns True if a process with the given image name is running."""
    if os.name != "nt":
        return False
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return False
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        target = exe_name.lower()
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == target:
                return True
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
        return False
    finally:
        k32.CloseHandle(snap)
