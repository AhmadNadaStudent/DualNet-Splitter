"""
Network inspection and metric management module.
Uses PowerShell / WMI to detect adapters, IP addresses, and manage interface metrics.
"""

import subprocess
import json
import re
import concurrent.futures
import base64
import ctypes
from typing import Optional, Dict, Any, Tuple

from engine.winutil import icmp_ping

def run_powershell(cmd: str) -> str:
    """Executes a PowerShell command and returns the trimmed output."""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True,
            text=True,
            timeout=8
        )
        return proc.stdout.strip()
    except Exception as e:
        return ""

def get_network_interfaces() -> Dict[str, Dict[str, Any]]:
    """
    Returns information about network adapters, focusing on Wi-Fi and Ethernet.
    Filters hardware description to avoid TAP/VPN virtual adapters.
    """
    ps_cmd = """
    $adapters = Get-NetAdapter -ErrorAction SilentlyContinue | ForEach-Object {
        $name = $_.Name
        $desc = $_.InterfaceDescription
        $status = $_.Status
        $ifIndex = $_.InterfaceIndex
        $ipObj = Get-NetIPAddress -InterfaceIndex $ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -First 1
        $ip = if ($ipObj) { $ipObj.IPAddress } else { "" }
        $metricObj = Get-NetIPInterface -InterfaceIndex $ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -First 1
        $metric = if ($metricObj) { $metricObj.InterfaceMetric } else { 0 }

        [PSCustomObject]@{
            InterfaceAlias = $name
            InterfaceDescription = $desc
            InterfaceIndex = $ifIndex
            InterfaceType = [int]$_.InterfaceType
            HardwareInterface = [bool]$_.HardwareInterface
            InterfaceMetric = $metric
            AutomaticMetric = if ($metricObj) { [string]$metricObj.AutomaticMetric } else { "" }
            ConnectionState = [string]$status
            Enabled = ($_.AdminStatus -ne 2 -and $status -ne 'Disabled')
            IPAddress = $ip
        }
    }
    $adapters | ConvertTo-Json -Compress
    """
    out = run_powershell(ps_cmd)
    adapters = {}
    if not out:
        return adapters

    try:
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        for item in data:
            alias = item.get("InterfaceAlias", "")
            desc = item.get("InterfaceDescription", "")
            adapters[alias] = {
                "alias": alias,
                "desc": desc,
                "index": item.get("InterfaceIndex", 0),
                "interface_type": item.get("InterfaceType", 0),
                "hardware": item.get("HardwareInterface"),
                "metric": item.get("InterfaceMetric", 0),
                "automatic_metric": {"enabled": True, "disabled": False}.get(str(item.get("AutomaticMetric", "")).lower()),
                "enabled": item.get("Enabled", item.get("ConnectionState") != "Disabled"),
                "state": item.get("ConnectionState", "Disconnected"),
                "ip": item.get("IPAddress", "")
            }
    except Exception:
        pass

    return adapters

def find_primary_adapters() -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Detects the Wi-Fi and Physical Ethernet adapters.
    Returns (wifi_adapter, ethernet_adapter).
    """
    adapters = get_network_interfaces()
    candidates = {"wifi": [], "ethernet": []}

    for alias, info in adapters.items():
        desc = info.get("desc", "").lower()
        lower_alias = alias.lower()

        # Skip VPN / Virtual TAP / VMWare / VirtualBox adapters
        if info.get("hardware") is False or any(v in desc or v in lower_alias for v in ["tap-windows", "vpn", "virtual", "hyper-v", "loopback", "wsl", "bluetooth", "dualnettun"]):
            continue

        kind = info.get("interface_type")
        # IANA ifType values are independent of adapter vendor and display language.
        if kind == 71:
            candidates["wifi"].append(info)
        elif kind == 6:
            candidates["ethernet"].append(info)
        elif not kind:
            # Compatibility fallback for drivers that omit ifType.
            if any(v in lower_alias or v in desc for v in ("wi-fi", "wifi", "wireless", "802.11", "wlan")):
                candidates["wifi"].append(info)
            elif any(v in lower_alias or v in desc for v in ("ethernet", "gigabit", "gbe", "lan")):
                candidates["ethernet"].append(info)

    def best(items):
        def rank(info):
            usable_ip = bool(info.get("ip") and not info["ip"].startswith(("169.254.", "127.")))
            return (info.get("state") == "Up", usable_ip,
                    -int(info.get("metric") or 0), -int(info.get("index") or 0))
        return max(items, key=rank) if items else None

    return best(candidates["wifi"]), best(candidates["ethernet"])


def adapter_enabled(adapter) -> bool:
    """An enabled adapter may be unplugged or waiting for Wi-Fi / DHCP."""
    return bool(adapter and adapter.get("enabled", adapter.get("state") != "Disabled"))


def detect_metric_preset(wifi, ethernet) -> str:
    """Describe observed IPv4 settings, including AutomaticMetric state."""
    if not wifi or not ethernet:
        return "unknown"
    w_auto, e_auto = wifi.get("automatic_metric"), ethernet.get("automatic_metric")
    if w_auto is None or e_auto is None:
        return "unknown"
    if w_auto is True and e_auto is True:
        return "reset"
    if w_auto is False and e_auto is False:
        if (wifi.get("metric"), ethernet.get("metric")) == (10, 50):
            return "gaming"
        if (wifi.get("metric"), ethernet.get("metric")) == (50, 10):
            return "download"
    return "custom"


def run_network_command(command: str):
    """Run a checked network change, preserving an elevated child's exit code."""
    command = "$ErrorActionPreference='Stop'; " + command
    if ctypes.windll.shell32.IsUserAnAdmin():
        return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                              capture_output=True, text=True, timeout=30)
    encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
        "$ErrorActionPreference='Stop'; $p = Start-Process powershell -Verb RunAs -WindowStyle Hidden -Wait -PassThru "
        f"-ArgumentList '-NoProfile -NonInteractive -EncodedCommand {encoded}'; exit $p.ExitCode"],
        capture_output=True, text=True, timeout=60)


def set_adapter_enabled(index: int, enabled: bool):
    """Target a single physical adapter by its numeric index."""
    if int(index) <= 0:
        raise ValueError("Index adapter tidak valid.")
    action = "Enable-NetAdapter" if enabled else "Disable-NetAdapter"
    return run_network_command(
        f"$adapter = Get-NetAdapter -IncludeHidden -EA Stop | Where-Object {{ $_.ifIndex -eq {int(index)} }}; "
        "if (-not $adapter) { throw 'Adapter tidak ditemukan. Klik Refresh.' }; "
        f"$adapter | {action} -Confirm:$false -EA Stop")

def measure_ping(source_ip: str, target: str = "1.1.1.1", timeout_ms: int = 800) -> Optional[float]:
    """
    Measures ping latency (in ms) through a specific source IP interface.
    Returns average ping in milliseconds or None if unreachable.
    """
    if not source_ip or source_ip.startswith("169.254"):
        return None

    # Silent native ICMP (IcmpSendEcho2Ex) - no ping.exe / console window is spawned.
    return icmp_ping(source_ip, target, timeout_ms)

def measure_both_pings(wifi_ip: Optional[str], eth_ip: Optional[str], target: str = "1.1.1.1", timeout_ms: int = 800) -> Tuple[Optional[float], Optional[float]]:
    """
    Measures both Wi-Fi and Ethernet pings concurrently to ensure sub-second latency.
    Falls back to Google DNS (8.8.8.8) if target fails on an active adapter.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f_wifi = executor.submit(measure_ping, wifi_ip, target, timeout_ms) if wifi_ip else None
        f_eth = executor.submit(measure_ping, eth_ip, target, timeout_ms) if eth_ip else None

        wifi_res = f_wifi.result() if f_wifi else None
        eth_res = f_eth.result() if f_eth else None

        # Fallback to 8.8.8.8 if 1.1.1.1 timed out on active interface
        if wifi_ip and wifi_res is None and target != "8.8.8.8":
            wifi_res = measure_ping(wifi_ip, "8.8.8.8", timeout_ms)
        if eth_ip and eth_res is None and target != "8.8.8.8":
            eth_res = measure_ping(eth_ip, "8.8.8.8", timeout_ms)

        return wifi_res, eth_res

def generate_metric_ps_script(wifi_metric: int = 10, ethernet_metric: int = 50, automatic: bool = False) -> str:
    """
    Generates PowerShell command script to enforce interface metrics.
    Wi-Fi = 10 (Highest priority default route for OS & Games)
    Ethernet = 50 (Secondary route for OS, used by custom proxy)
    """
    wifi, ethernet = find_primary_adapters()
    if not wifi or not ethernet:
        raise ValueError("Both a physical Wi-Fi and Ethernet adapter are required.")
    commands = []
    for adapter, metric in ((wifi, wifi_metric), (ethernet, ethernet_metric)):
        index = int(adapter["index"])
        if index <= 0:
            raise ValueError("Invalid adapter index. Refresh adapters and try again.")
        setting = "-AutomaticMetric Enabled" if automatic else f"-AutomaticMetric Disabled -InterfaceMetric {int(metric)}"
        commands.append(f"Set-NetIPInterface -InterfaceIndex {index} -AddressFamily IPv4 {setting} -ErrorAction Stop")
    return "; ".join(commands)
