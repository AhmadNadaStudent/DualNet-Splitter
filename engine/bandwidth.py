"""Per-adapter traffic rates from Windows' 64-bit interface counters.

GetIfEntry2 / MIB_IF_ROW2 layout:
https://learn.microsoft.com/windows/win32/api/netioapi/ns-netioapi-mib_if_row2
"""

import ctypes
import os
import time
from dataclasses import dataclass
from typing import Optional, Tuple


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


class _MIB_IF_ROW2(ctypes.Structure):
    _fields_ = [
        ("InterfaceLuid", ctypes.c_uint64),
        ("InterfaceIndex", ctypes.c_uint32),
        ("InterfaceGuid", _GUID),
        ("Alias", ctypes.c_wchar * 257),
        ("Description", ctypes.c_wchar * 257),
        ("PhysicalAddressLength", ctypes.c_uint32),
        ("PhysicalAddress", ctypes.c_ubyte * 32),
        ("PermanentPhysicalAddress", ctypes.c_ubyte * 32),
        ("Mtu", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TunnelType", ctypes.c_uint32),
        ("MediaType", ctypes.c_uint32),
        ("PhysicalMediumType", ctypes.c_uint32),
        ("AccessType", ctypes.c_uint32),
        ("DirectionType", ctypes.c_uint32),
        ("InterfaceAndOperStatusFlags", ctypes.c_ubyte),
        ("OperStatus", ctypes.c_uint32),
        ("AdminStatus", ctypes.c_uint32),
        ("MediaConnectState", ctypes.c_uint32),
        ("NetworkGuid", _GUID),
        ("ConnectionType", ctypes.c_uint32),
    ] + [(name, ctypes.c_uint64) for name in (
        "TransmitLinkSpeed", "ReceiveLinkSpeed", "InOctets", "InUcastPkts",
        "InNUcastPkts", "InDiscards", "InErrors", "InUnknownProtos",
        "InUcastOctets", "InMulticastOctets", "InBroadcastOctets", "OutOctets",
        "OutUcastPkts", "OutNUcastPkts", "OutDiscards", "OutErrors",
        "OutUcastOctets", "OutMulticastOctets", "OutBroadcastOctets", "OutQLen",
    )]


_get_if_entry = None
if os.name == "nt":
    _get_if_entry = ctypes.WinDLL("iphlpapi.dll").GetIfEntry2
    _get_if_entry.argtypes = [ctypes.POINTER(_MIB_IF_ROW2)]
    _get_if_entry.restype = ctypes.c_uint32


@dataclass(frozen=True)
class InterfaceCounters:
    identity: int
    received: int
    sent: int


def read_interface_counters(index: int) -> Optional[InterfaceCounters]:
    """Return counters for an up interface; no subprocess or TUN aggregation."""
    if _get_if_entry is None or not index:
        return None
    row = _MIB_IF_ROW2()
    row.InterfaceIndex = index
    if _get_if_entry(ctypes.byref(row)) != 0 or row.OperStatus != 1:
        return None
    return InterfaceCounters(row.InterfaceLuid, row.InOctets, row.OutOctets)


class BandwidthSampler:
    """Compute bytes/second using actual elapsed time, rebasing on reconnect."""

    def __init__(self, reader=read_interface_counters, clock=time.monotonic):
        self.reader = reader
        self.clock = clock
        self.previous = {}

    def sample(self, source: str, adapter) -> Optional[Tuple[float, float]]:
        if not adapter or adapter.get("state") != "Up" or not adapter.get("index"):
            self.previous.pop(source, None)
            return None
        index = int(adapter["index"])
        try:
            counters = self.reader(index)
        except (OSError, ValueError):
            counters = None
        if counters is None:
            self.previous.pop(source, None)
            return None
        now = self.clock()
        previous = self.previous.get(source)
        self.previous[source] = (index, counters, now)
        if previous is None:
            return None
        old_index, old, timestamp = previous
        elapsed = now - timestamp
        if (old_index != index or old.identity != counters.identity or elapsed <= 0
                or counters.received < old.received or counters.sent < old.sent):
            return None
        return ((counters.received - old.received) / elapsed,
                (counters.sent - old.sent) / elapsed)


def format_rate(bytes_per_second: float) -> str:
    """Decimal units, so MB/s means one million bytes per second."""
    value = max(0.0, bytes_per_second)
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if value < 1000 or unit == "GB/s":
            return f"{value:.1f} {unit}"
        value /= 1000
