"""
Zero-Configuration Smart Transparent Virtual Adapter (TUN Mode) Manager.
Utilizes Sing-box core and Wintun driver to route traffic seamlessly per-process.
"""

import os
import sys
import json
import time
import ctypes
import io
import urllib.request
import zipfile
import subprocess
import hashlib
import platform
import struct
from typing import Optional, Dict, Any, Tuple, List

from engine.winutil import APP_DIR, process_exists

BIN_DIR = os.path.join(APP_DIR, "engine", "bin")
SING_BOX_EXE = os.path.join(BIN_DIR, "sing-box.exe")
WINTUN_DLL = os.path.join(BIN_DIR, "wintun.dll")
CONFIG_PATH = os.path.join(BIN_DIR, "tun_config.json")
LOG_PATH = os.path.join(BIN_DIR, "sing-box.log")
RULES_PATH = os.path.join(APP_DIR, "routing_rules.json")

DEFAULT_RULES = {
    "description": "DualNet Smart Process Routing Rules (TUN Virtual Adapter)",
    "download_processes": [
        "idman.exe",
        "steam.exe",
        "steamservice.exe",
        "steamwebhelper.exe",
        "qbittorrent.exe",
        "utorrent.exe",
        "BitTorrent.exe",
        "Transmission.exe",
        "EpicGamesLauncher.exe",
        "Battle.net.exe",
        "Agent.exe",
        "Origin.exe",
        "EADesktop.exe",
        "EABackgroundService.exe",
        "GOG Galaxy.exe",
        "GalaxyClient.exe",
        "RiotClientServices.exe",
        "XboxApp.exe",
        "GamingServices.exe",
        "aria2c.exe",
        "wget.exe",
        "curl.exe"
    ],
    "gaming_processes": [
        "cs2.exe",
        "valorant.exe",
        "vgc.exe",
        "VALORANT-Win64-Shipping.exe",
        "dota2.exe",
        "league of legends.exe",
        "leagueclient.exe",
        "leagueclientux.exe",
        "r5apex.exe",
        "GenshinImpact.exe",
        "StarRail.exe",
        "ZenlessZoneZero.exe",
        "Overwatch.exe",
        "FortniteClient-Win64-Shipping.exe",
        "RobloxPlayerBeta.exe",
        "Discord.exe",
        "ts3client_win64.exe"
    ],
    "default_outbound": "wifi"
}

def is_admin() -> bool:
    """Checks whether the script is currently running with Administrator privileges."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

SING_BOX_VERSION = "1.14.4"
SING_BOX_SHA256 = {
    "amd64": "99185d75d314a07dbba4a130c8ad986cbeb629a07927975636b341ca61f991df",
    "arm64": "8cd0a4d0cd8cd480be5731ac31f17038969a42d12add9feb6e031fd43d85bbae",
    "386": "7994c40c5c1316777bdd29216a5dd7f134f894fad0afaa1efe7b872e60ed1dd9",
}
WINTUN_SHA256 = "07c256185d6ee3652e09fa55c0b673e2624b565e02c4b9091c79ca7d2f24ef51"


def binary_architecture() -> str:
    """Match the Python process architecture, including Windows emulation."""
    if struct.calcsize("P") == 4:
        return "386"
    machine = os.environ.get("PROCESSOR_ARCHITECTURE", platform.machine()).lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("amd64", "x86_64"):
        return "amd64"
    raise RuntimeError(f"Unsupported processor architecture: {machine}")


def _install_archive(url, digest, member_suffix, destination):
    """Verify the official ZIP before atomically installing one exact member."""
    req = urllib.request.Request(url, headers={"User-Agent": "DualNet-Splitter"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        content = resp.read()
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Download checksum mismatch; installation cancelled.")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = [name for name in archive.namelist() if name.endswith(member_suffix)]
        if len(names) != 1:
            raise ValueError("Expected runtime binary was not found in the official archive.")
        temporary = destination + ".tmp"
        try:
            with open(temporary, "wb") as stream:
                stream.write(archive.read(names[0]))
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def ensure_binaries(progress_callback=None) -> bool:
    """Ensures sing-box.exe and wintun.dll exist in engine/bin. Downloads them if absent."""
    os.makedirs(BIN_DIR, exist_ok=True)
    if os.path.exists(SING_BOX_EXE) and os.path.exists(WINTUN_DLL):
        return True

    try:
        arch = binary_architecture()
        if not os.path.exists(SING_BOX_EXE):
            if progress_callback:
                progress_callback("Mengunduh Sing-box Core resmi (v1.14.4)...")
            url = f"https://github.com/SagerNet/sing-box/releases/download/v{SING_BOX_VERSION}/sing-box-{SING_BOX_VERSION}-windows-{arch}.zip"
            _install_archive(url, SING_BOX_SHA256[arch], "/sing-box.exe", SING_BOX_EXE)

        if not os.path.exists(WINTUN_DLL):
            if progress_callback:
                progress_callback("Mengunduh driver Wintun resmi (WireGuard)...")
            url = "https://www.wintun.net/builds/wintun-0.14.1.zip"
            wintun_arch = "x86" if arch == "386" else arch
            _install_archive(url, WINTUN_SHA256, f"/{wintun_arch}/wintun.dll", WINTUN_DLL)

        return os.path.exists(SING_BOX_EXE) and os.path.exists(WINTUN_DLL)
    except Exception as e:
        if progress_callback:
            progress_callback(f"Gagal mengunduh binary: {e}")
        return False

def load_rules() -> Dict[str, Any]:
    """Loads routing rules from routing_rules.json or creates default if missing."""
    if not os.path.exists(RULES_PATH):
        try:
            with open(RULES_PATH, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_RULES, f, indent=4)
        except Exception:
            return DEFAULT_RULES
        return DEFAULT_RULES

    try:
        with open(RULES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return DEFAULT_RULES

def save_rules(rules: Dict[str, Any]):
    """Saves routing rules to routing_rules.json."""
    try:
        with open(RULES_PATH, "w", encoding="utf-8") as f:
            json.dump(rules, f, indent=4)
    except Exception:
        pass

def is_singbox_running() -> bool:
    """Checks whether sing-box process is actively running in the OS (no tasklist.exe spawn)."""
    try:
        return process_exists("sing-box.exe")
    except Exception:
        return False

class TUNManager:
    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.is_running = is_singbox_running()
        self.rules = load_rules()
        self.last_error = ""

    def reload_rules(self):
        self.rules = load_rules()

    def generate_config(self, wifi_alias: str, eth_alias: Optional[str]) -> Dict[str, Any]:
        """Generates sing-box JSON configuration for transparent TUN process routing."""
        self.reload_rules()
        down_procs = self.rules.get("download_processes", [])
        game_procs = self.rules.get("gaming_processes", [])

        # Inbound TUN configuration
        inbounds = [
            {
                "type": "tun",
                "tag": "tun-in",
                "interface_name": "DualNetTUN",
                "address": ["172.19.0.1/30"],
                "auto_route": True,
                "strict_route": False,
                "stack": "mixed"
            }
        ]

        # Outbounds
        outbounds = [
            {
                "type": "direct",
                "tag": "wifi_out",
                "bind_interface": wifi_alias
            }
        ]

        if eth_alias:
            outbounds.append({
                "type": "direct",
                "tag": "lan_out",
                "bind_interface": eth_alias
            })

        # Routing rules
        route_rules = [
            {
                "action": "route",
                "protocol": "dns",
                "outbound": "wifi_out"
            }
        ]

        # If Ethernet is available, route download processes to LAN
        if eth_alias and down_procs:
            route_rules.append({
                "action": "route",
                "process_name": down_procs,
                "outbound": "lan_out"
            })

        if game_procs:
            route_rules.append({
                "action": "route",
                "process_name": game_procs,
                "outbound": "wifi_out"
            })

        cfg = {
            "log": {
                "level": "info",
                "timestamp": True
            },
            "dns": {
                "servers": [
                    {
                        "tag": "dns-direct",
                        "type": "udp",
                        "server": "1.1.1.1",
                        "detour": "wifi_out"
                    }
                ]
            },
            "inbounds": inbounds,
            "outbounds": outbounds,
            "route": {
                "rules": route_rules,
                "final": "wifi_out",
                "auto_detect_interface": True
            }
        }
        return cfg

    def start(self, wifi_alias: str, eth_alias: Optional[str]) -> Tuple[bool, str]:
        """Starts the TUN Virtual Adapter."""
        if self.is_running and self.process and self.process.poll() is None:
            return True, "Virtual Adapter (TUN) sudah berjalan."

        if not is_admin():
            return False, "Diperlukan izin Administrator (Run as Administrator) untuk mengaktifkan Virtual Adapter!"

        if not ensure_binaries():
            return False, "File sing-box.exe atau wintun.dll belum tersedia di engine/bin/."

        cfg = self.generate_config(wifi_alias, eth_alias)
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except Exception as e:
            return False, f"Gagal membuat file config TUN: {e}"

        # Test config validity first
        check_res = subprocess.run(
            [SING_BOX_EXE, "check", "-c", CONFIG_PATH],
            capture_output=True,
            text=True,
            cwd=BIN_DIR
        )
        if check_res.returncode != 0:
            err = check_res.stderr.strip() or check_res.stdout.strip()
            self.last_error = err
            return False, f"Validasi config TUN gagal: {err}"

        # Spawn sing-box in background
        try:
            log_file = open(LOG_PATH, "w", encoding="utf-8")
            creation_flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
            self.process = subprocess.Popen(
                [SING_BOX_EXE, "run", "-c", CONFIG_PATH],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=BIN_DIR,
                creationflags=creation_flags
            )

            # The child holds its own handle; close the parent's duplicate.
            log_file.close()
            # Wait briefly to ensure it initializes properly
            time.sleep(1.5)
            if self.process.poll() is not None:
                log_file.close()
                with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
                    log_content = f.read().strip()
                self.is_running = False
                self.last_error = log_content
                return False, f"Virtual Adapter gagal start: {log_content[-200:] if len(log_content) > 200 else log_content}"

            self.is_running = True
            return True, "Virtual Adapter (TUN) BERHASIL aktif! Mode Zero-Config berjalan."
        except Exception as e:
            self.is_running = False
            self.last_error = str(e)
            return False, f"Error saat memulai TUN: {e}"

    def stop(self) -> Tuple[bool, str]:
        """Stops the TUN Virtual Adapter cleanly."""
        self.is_running = False
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None

        return True, "Virtual Adapter (TUN) telah dinonaktifkan."

    def check_health(self) -> bool:
        """Verifies if the TUN background process is still active."""
        if self.process:
            if self.process.poll() is not None:
                self.is_running = False
                return False
            self.is_running = True
            return True
        self.is_running = False
        return False
