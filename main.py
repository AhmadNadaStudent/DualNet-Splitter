"""
DualNet Splitter & Routing Handler
Separates Gaming/OS traffic (Wi-Fi Low Latency) and Download traffic (LAN High Speed).
Supports both Zero-Config Virtual Adapter (TUN Mode) and Local Outbound Proxy Mode.
"""

import sys
import os
import time
import threading
import asyncio
import subprocess
from typing import Optional

try:
    import msvcrt
    HAS_MSVCRT = True
except ImportError:
    HAS_MSVCRT = False

from colorama import init, Fore, Back, Style
from engine.network import find_primary_adapters, measure_ping, measure_both_pings, run_powershell, generate_metric_ps_script, run_network_command
from engine.proxy import DualNetProxy
from engine.apps import find_steam
from engine.tun_manager import TUNManager, is_admin, RULES_PATH
from engine.autostart import is_autostart_installed, install_autostart, uninstall_autostart

init(autoreset=True)

class DualNetApp:
    def __init__(self):
        self.wifi_adapter = None
        self.eth_adapter = None
        self.wifi_ping = None
        self.eth_ping = None
        self.proxy = None
        self.proxy_thread = None
        self.proxy_loop = None
        self.tun = TUNManager()
        self.is_monitoring = True
        self.status_message = "Ready."
        self.last_update_time = ""
        self._needs_clear = True

    def refresh_adapters(self):
        self.wifi_adapter, self.eth_adapter = find_primary_adapters()

    def get_ethernet_ip(self) -> Optional[str]:
        if self.eth_adapter and self.eth_adapter.get("ip"):
            ip = self.eth_adapter["ip"]
            if not ip.startswith("169.254") and not ip.startswith("127."):
                return ip
        return None

    def start_tun(self):
        """Starts Zero-Config TUN Virtual Adapter."""
        self.refresh_adapters()
        if not self.wifi_adapter:
            self.status_message = "Gagal: Adapter Wi-Fi tidak ditemukan!"
            return

        wifi_alias = self.wifi_adapter.get("alias", "Wi-Fi")
        eth_alias = self.eth_adapter.get("alias", "Ethernet") if self.eth_adapter else None

        success, msg = self.tun.start(wifi_alias, eth_alias)
        self.status_message = msg

    def stop_tun(self):
        """Stops Zero-Config TUN Virtual Adapter."""
        success, msg = self.tun.stop()
        self.status_message = msg

    def toggle_tun(self):
        if self.tun.is_running:
            self.stop_tun()
        else:
            self.start_tun()

    def start_proxy(self):
        if self.proxy and self.proxy.is_running:
            return


        self.proxy = DualNetProxy(
            get_outbound_ip_fn=self.get_ethernet_ip,
            http_port=8080,
            socks_port=1080
        )

        def _run_loop():
            self.proxy_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.proxy_loop)
            try:
                success, msg = self.proxy_loop.run_until_complete(self.proxy.start())
                self.status_message = msg
                if success:
                    self.proxy_loop.run_forever()
            except Exception as e:
                self.status_message = f"Proxy error: {e}"

        self.proxy_thread = threading.Thread(target=_run_loop, daemon=True)
        self.proxy_thread.start()

    def stop_proxy(self):
        if self.proxy and self.proxy.is_running:
            if self.proxy_loop and self.proxy_loop.is_running():
                asyncio.run_coroutine_threadsafe(self.proxy.stop(), self.proxy_loop)
                self.proxy_loop.call_soon_threadsafe(self.proxy_loop.stop())
            self.status_message = "Proxy engine stopped."

    def _apply_metric_preset(self, wifi_metric=10, ethernet_metric=50, automatic=False):
        try:
            result = run_network_command(generate_metric_ps_script(wifi_metric, ethernet_metric, automatic))
            self.status_message = ("Interface metrics updated." if result.returncode == 0 else
                                   result.stderr.strip() or "Metric change failed or was cancelled.")
            self.refresh_adapters()
        except Exception as error:
            self.status_message = f"Metric change failed: {error}"

    def apply_metrics(self):
        self._apply_metric_preset()

    def apply_lan_full_metrics(self):
        self._apply_metric_preset(50, 10)

    def reset_metrics(self):
        self._apply_metric_preset(automatic=True)

    def monitor_loop(self):
        last_adapter_refresh = 0
        while self.is_monitoring:
            now = time.time()
            if now - last_adapter_refresh >= 8:
                self.refresh_adapters()
                last_adapter_refresh = time.time()

            wifi_ip = self.wifi_adapter.get("ip") if self.wifi_adapter else None
            eth_ip = self.get_ethernet_ip()

            self.wifi_ping, self.eth_ping = measure_both_pings(wifi_ip, eth_ip)
            self.last_update_time = time.strftime("%H:%M:%S")

            if self.proxy:
                self.proxy.stats.update_speed()

            if self.tun:
                self.tun.check_health()

            time.sleep(1)

    def render_dashboard(self) -> str:
        admin_tag = f"{Fore.GREEN}[ADMIN]" if is_admin() else f"{Fore.YELLOW}[USER]"
        timestamp_str = f" | {Fore.WHITE}{self.last_update_time}{Style.RESET_ALL}" if self.last_update_time else ""

        lines = []
        lines.append(f"{Fore.CYAN}{Style.BRIGHT}==========================================================================")
        lines.append(f"{Fore.GREEN}{Style.BRIGHT}   DUALNET SPLITTER - GAMING & DOWNLOAD ROUTING HANDLER {admin_tag}{timestamp_str}")
        lines.append(f"{Fore.CYAN}{Style.BRIGHT}==========================================================================")
        lines.append(f"{Style.DIM}Pemisah jalur: Wi-Fi (Ping Stabil / Gaming) & LAN Cable (Kencang / Download)\n")

        # 1. Wi-Fi Status
        lines.append(f"{Fore.YELLOW}{Style.BRIGHT}[1] WI-FI (JALUR UTAMA / PING STABIL / GAMING):")
        if self.wifi_adapter:
            alias = self.wifi_adapter.get("alias", "Wi-Fi")
            ip = self.wifi_adapter.get("ip", "No IP")
            metric = self.wifi_adapter.get("metric", "Auto")
            state = self.wifi_adapter.get("state", "Unknown")

            state_color = Fore.GREEN if state == "Up" else Fore.RED
            metric_tag = f"{Fore.GREEN}(Optimal: 10)" if metric == 10 else f"{Fore.YELLOW}(Saran: Set ke 10)"

            if self.wifi_ping is not None:
                ping_str = f"{self.wifi_ping:.1f} ms"
                ping_color = Fore.GREEN if self.wifi_ping < 50 else (Fore.YELLOW if self.wifi_ping < 100 else Fore.RED)
                live_indicator = f"{Fore.GREEN}● LIVE{Style.RESET_ALL}"
            elif state == "Up" and ip and not ip.startswith("169.254"):
                ping_str = "Timeout / RTO"
                ping_color = Fore.RED
                live_indicator = f"{Fore.RED}● RTO{Style.RESET_ALL}"
            else:
                ping_str = "Checking..."
                ping_color = Fore.YELLOW
                live_indicator = f"{Style.DIM}○ IDLE{Style.RESET_ALL}"

            lines.append(f"    Status   : {state_color}{state}{Style.RESET_ALL} | IP: {Fore.WHITE}{ip} | Metric: {Fore.CYAN}{metric} {metric_tag}")
            lines.append(f"    Ping     : {ping_color}{ping_str:<8}{Style.RESET_ALL} {live_indicator} [1.1.1.1] (Route: Valorant, CS2, Discord, Game)")
        else:
            lines.append(f"    {Fore.RED}Adapter Wi-Fi tidak ditemukan!")

        lines.append("")

        # 2. Ethernet Status
        lines.append(f"{Fore.BLUE}{Style.BRIGHT}[2] LAN ETHERNET (JALUR KENCANG / DOWNLOAD & STREAMING):")
        if self.eth_adapter:
            alias = self.eth_adapter.get("alias", "Ethernet")
            desc = self.eth_adapter.get("desc", "Ethernet adapter")
            ip = self.eth_adapter.get("ip", "No IP")
            metric = self.eth_adapter.get("metric", "Auto")
            state = self.eth_adapter.get("state", "Disconnected")

            is_connected = state == "Up" and ip and not ip.startswith("169.254")
            state_color = Fore.GREEN if is_connected else Fore.RED
            state_label = "Connected & Ready" if is_connected else "Belum Dicolok (Cable Unplugged)"

            metric_tag = f"{Fore.GREEN}(Optimal: 50)" if metric == 50 else f"{Fore.YELLOW}(Saran: Set ke 50)"

            if self.eth_ping is not None:
                ping_str = f"{self.eth_ping:.1f} ms"
                ping_color = Fore.GREEN if self.eth_ping < 50 else (Fore.YELLOW if self.eth_ping < 100 else Fore.RED)
                live_indicator = f"{Fore.GREEN}● LIVE{Style.RESET_ALL}"
            elif not is_connected:
                ping_str = "N/A (Belum Dicolok)"
                ping_color = Fore.WHITE
                live_indicator = f"{Style.DIM}○ IDLE{Style.RESET_ALL}"
            else:
                ping_str = "Timeout / RTO"
                ping_color = Fore.RED
                live_indicator = f"{Fore.RED}● RTO{Style.RESET_ALL}"

            lines.append(f"    Device   : {Fore.WHITE}{desc}")
            lines.append(f"    Status   : {state_color}{state_label}{Style.RESET_ALL} | IP: {Fore.WHITE}{ip} | Metric: {Fore.CYAN}{metric} {metric_tag}")
            lines.append(f"    Ping     : {ping_color}{ping_str:<8}{Style.RESET_ALL} {live_indicator} [1.1.1.1]")
        else:
            lines.append(f"    {Fore.RED}Adapter Ethernet tidak ditemukan!")

        lines.append("")

        # 3. Smart Virtual Adapter (TUN Mode)
        lines.append(f"{Fore.CYAN}{Style.BRIGHT}[3] SMART VIRTUAL ADAPTER (TUN MODE - ZERO CONFIG) ⭐:")
        autostart_active = is_autostart_installed()
        autostart_badge = f"{Fore.GREEN}ON (Otomatis saat Windows Nyala)" if autostart_active else f"{Fore.YELLOW}OFF (Manual)"
        if self.tun and self.tun.is_running:
            down_count = len(self.tun.rules.get("download_processes", []))
            game_count = len(self.tun.rules.get("gaming_processes", []))
            lines.append(f"    Status   : {Fore.GREEN}{Style.BRIGHT}AKTIF (Driver Wintun Active){Style.RESET_ALL} | Autostart: {autostart_badge}")
            lines.append(f"    Filter   : {Fore.WHITE}{down_count} apps ke LAN (Download) | {game_count} apps ke Wi-Fi (Gaming)")
            lines.append(f"    Fitur    : {Fore.GREEN}Otomatis tanpa perlu setting proxy di IDM / Steam / Browser!")
        else:
            lines.append(f"    Status   : {Fore.RED}NONAKTIF {Style.DIM}(Tekan 't' untuk mengaktifkan Zero-Config Mode) | Autostart: {autostart_badge}")

        lines.append("")

        # 4. Local Proxy Status (Legacy / Manual)
        lines.append(f"{Fore.MAGENTA}{Style.BRIGHT}[4] DOWNLOAD PROXY ENGINE (LEGACY / PORT 8080 & 1080):")
        if self.proxy and self.proxy.is_running:
            bound_ip = self.proxy.get_effective_outbound_ip()
            bound_str = f"{Fore.GREEN}{bound_ip} (Ethernet LAN)" if bound_ip else f"{Fore.YELLOW}Fallback (Menunggu kabel LAN dicolok)"
            down_mb = self.proxy.stats.current_download_speed / (1024 * 1024)
            up_mb = self.proxy.stats.current_upload_speed / (1024 * 1024)
            total_recv_mb = self.proxy.stats.bytes_recv / (1024 * 1024)

            lines.append(f"    Status   : {Fore.GREEN}AKTIF{Style.RESET_ALL} | HTTP Port: {Fore.CYAN}8080{Style.RESET_ALL} | SOCKS5 Port: {Fore.CYAN}1080")
            lines.append(f"    Outbound : {bound_str}")
            lines.append(f"    Speed    : Down: {Fore.GREEN}{down_mb:.2f} MB/s{Style.RESET_ALL} | Up: {Fore.CYAN}{up_mb:.2f} MB/s | Total Recv: {Fore.WHITE}{total_recv_mb:.2f} MB")
            lines.append(f"    Active   : {Fore.YELLOW}{self.proxy.stats.active_conns} koneksi proxy aktif")
        else:
            lines.append(f"    Status   : {Fore.RED}NONAKTIF")

        lines.append(f"\n{Fore.CYAN}--------------------------------------------------------------------------")
        lines.append(f"Pesan: {Fore.WHITE}{self.status_message}")
        lines.append(f"{Fore.CYAN}--------------------------------------------------------------------------")
        lines.append(f"{Fore.GREEN}{Style.BRIGHT}[t]{Style.RESET_ALL} Toggle Virtual Adapter (TUN Mode - Zero Config) {Fore.GREEN}[⭐ REKOMENDASI]")
        lines.append(f"{Fore.YELLOW}[a]{Style.RESET_ALL} Toggle Autostart Windows (Jalankan Otomatis saat PC Nyala)")
        lines.append(f"{Fore.YELLOW}[p]{Style.RESET_ALL} Toggle Local Proxy Engine (Port 8080 & 1080)")
        lines.append(f"{Fore.YELLOW}[e]{Style.RESET_ALL} Edit Aturan Aplikasi Game & Download (routing_rules.json)")
        lines.append(f"{Fore.YELLOW}[1]{Style.RESET_ALL} Set Mode Gaming (Wi-Fi=10 Stabil, LAN=50 Sekunder)")
        lines.append(f"{Fore.YELLOW}[2]{Style.RESET_ALL} Set Mode Full Download (LAN=10 Kencang, Wi-Fi=50)")
        lines.append(f"{Fore.YELLOW}[3]{Style.RESET_ALL} Reset Metric ke Otomatis Windows")
        lines.append(f"{Fore.YELLOW}[5]{Style.RESET_ALL} Buka Steam Lewat Jalur LAN Kencang")
        lines.append(f"{Fore.YELLOW}[6]{Style.RESET_ALL} Panduan Penggunaan")
        lines.append(f"{Fore.YELLOW}[r]{Style.RESET_ALL} Refresh Status")
        lines.append(f"{Fore.YELLOW}[q]{Style.RESET_ALL} Keluar")
        lines.append(f"{Fore.CYAN}==========================================================================")
        lines.append(f"Pilih menu [t / a / p / e / 1-6 / r / q]: ")
        return "\n".join(lines)

    def print_dashboard(self):
        content = self.render_dashboard()
        if self._needs_clear:
            os.system("cls" if os.name == "nt" else "clear")
            self._needs_clear = False
            sys.stdout.write(content)
        else:
            sys.stdout.write("\033[H" + content + "\033[J")
        sys.stdout.flush()

    def open_rules_file(self):
        """Opens routing_rules.json in Notepad for user customization."""
        try:
            subprocess.run(["notepad.exe", RULES_PATH])
            self.tun.reload_rules()
            self.status_message = "File routing_rules.json telah dimuat ulang."
        except Exception as e:
            self.status_message = f"Gagal membuka routing_rules.json: {e}"
        self._needs_clear = True

    def show_guide(self):
        os.system("cls" if os.name == "nt" else "clear")
        print(f"{Fore.GREEN}{Style.BRIGHT}==========================================================================")
        print(f"                     PANDUAN PENGGUNAAN DUALNET SPLITTER")
        print(f"{Fore.GREEN}{Style.BRIGHT}==========================================================================\n")
        print(f"{Fore.CYAN}{Style.BRIGHT}⭐ CARA 1: MODE VIRTUAL ADAPTER (TUN MODE - ZERO CONFIG)")
        print("   - Tekan menu [t] di Dashboard.")
        print("   - Virtual adapter berbasis Wintun akan aktif.")
        print("   - SEMUA otomatis! IDM, Steam, qBittorrent, Epic Games langsung dialihkan")
        print("     ke Kabel LAN tanpa perlu setting proxy apapun di aplikasinya.")
        print("   - Game (Valorant, CS2, Dota, Apex, Discord) tetap 100% di Wi-Fi sehingga")
        print("     ping Anda tetap stabil dan bebas ping spike.")
        print("   - Anda bisa menambah/mengubah daftar game atau aplikasi download lewat menu [e].\n")

        print(f"{Fore.CYAN}{Style.BRIGHT}⚙️ CARA 2: MODE MANUAL PROXY (LEGACY)")
        print("   - Tekan menu [p] di Dashboard untuk mengaktifkan Proxy Engine.")
        print("   - IDM   : Options -> Proxy/Socks -> HTTP 127.0.0.1 Port 8080.")
        print("   - Steam : Gunakan shortcut Desktop dengan tambahan target:")
        print("             -proxy-server=\"http://127.0.0.1:8080\" atau pilih menu [5].")
        print("   - Torrent: Tools -> Options -> Advanced -> Network Interface -> Ethernet.\n")

        input(f"\n{Fore.YELLOW}Tekan [Enter] untuk kembali ke Dashboard...")
        self._needs_clear = True

    def launch_steam_proxy(self):
        steam_path = find_steam()
        if not steam_path:
            self.status_message = "Steam was not found. Install Steam or add it to PATH."
            return
        subprocess.Popen([steam_path, "-proxy-server=http://127.0.0.1:8080"])
        self.status_message = "Steam berhasil diluncurkan dengan proxy LAN (127.0.0.1:8080)!"

    def run(self):
        self.refresh_adapters()
        wifi_ip = self.wifi_adapter.get("ip") if self.wifi_adapter else None
        eth_ip = self.get_ethernet_ip()
        self.wifi_ping, self.eth_ping = measure_both_pings(wifi_ip, eth_ip)
        self.last_update_time = time.strftime("%H:%M:%S")

        self.start_proxy()

        # Start background monitor thread
        mon_thread = threading.Thread(target=self.monitor_loop, daemon=True)
        mon_thread.start()

        self._needs_clear = True

        try:
            while self.is_monitoring:
                self.print_dashboard()

                # Non-blocking key check: wait up to 1.0s per cycle
                choice = None
                start_wait = time.time()
                while time.time() - start_wait < 1.0:
                    if HAS_MSVCRT and msvcrt.kbhit():
                        ch = msvcrt.getwch()
                        if ch in ('\x00', '\xe0'):
                            if msvcrt.kbhit():
                                msvcrt.getwch()
                            continue
                        choice = ch.lower()
                        break
                    time.sleep(0.05)

                if not HAS_MSVCRT and choice is None:
                    try:
                        choice = input().strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        choice = "q"

                if not choice or choice in ('\r', '\n', ' '):
                    continue

                if choice == "t":
                    self.toggle_tun()
                    time.sleep(1)
                elif choice == "a":
                    if is_autostart_installed():
                        success, msg = uninstall_autostart()
                    else:
                        success, msg = install_autostart()
                    self.status_message = msg
                    time.sleep(1)
                elif choice == "p":
                    if self.proxy and self.proxy.is_running:
                        self.stop_proxy()
                    else:
                        self.start_proxy()
                    time.sleep(1)
                elif choice == "e":
                    self.open_rules_file()
                elif choice == "1":
                    self.apply_metrics()
                    time.sleep(1)
                elif choice == "2":
                    self.apply_lan_full_metrics()
                    time.sleep(1)
                elif choice == "3":
                    self.reset_metrics()
                    time.sleep(1)
                elif choice == "5":
                    self.launch_steam_proxy()
                    time.sleep(2)
                elif choice == "6":
                    self.show_guide()
                elif choice == "r":
                    self.refresh_adapters()
                    wifi_ip = self.wifi_adapter.get("ip") if self.wifi_adapter else None
                    eth_ip = self.get_ethernet_ip()
                    self.wifi_ping, self.eth_ping = measure_both_pings(wifi_ip, eth_ip)
                    self.status_message = "Refreshed."
                elif choice == "q":
                    print("\nMenutup DualNet...")
                    self.is_monitoring = False
                    self.stop_tun()
                    self.stop_proxy()
                    break
        except KeyboardInterrupt:
            self.is_monitoring = False
            self.stop_tun()
            self.stop_proxy()
            print("\nKeluar.")

if __name__ == "__main__":
    app = DualNetApp()
    app.run()
