"""
DualNet Splitter - Desktop App (HUD style UI)
Wi-Fi (gaming / low latency) + LAN (download / high throughput) traffic splitter.

* Pure tkinter Canvas UI with light/dark themes and a monitoring view.
* Silent monitoring: native ICMP ping (no ping.exe), no tasklist.exe, every helper
  subprocess spawned with CREATE_NO_WINDOW -> zero console flashing.
* Run with --autostart to start TUN in the tray (used by Task Scheduler).
"""

import os
import sys
import time
import ctypes
import asyncio
import threading
import subprocess
import queue
import json
from collections import deque
from typing import Optional

import tkinter as tk
import tkinter.font as tkfont

import engine  # noqa: F401  (patches subprocess -> CREATE_NO_WINDOW)
from engine.winutil import APP_DIR
from engine.network import (find_primary_adapters, measure_both_pings, adapter_enabled,
                            detect_metric_preset, run_network_command, set_adapter_enabled)
from engine.proxy import DualNetProxy
from engine.apps import find_steam
from engine.tun_manager import TUNManager, is_admin, RULES_PATH
from engine.autostart import is_autostart_installed, install_autostart, uninstall_autostart
from engine.bandwidth import BandwidthSampler, format_rate
from ui_preferences import PALETTES, LIGHT, display_settings, save_settings, translate

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except Exception:
    HAS_TRAY = False


# ---------------------------------------------------------------------------
# Palette & fonts
# ---------------------------------------------------------------------------
C = LIGHT  # public light palette for preview tools
COMPACT_W, COMPACT_H = 480, 180

W, H = 1120, 700
_instance_mutex = None
_show_event = None
SHOW_EVENT_NAME = "Local\\DualNetSplitterShow"
UI_STATE_PATH = os.path.join(APP_DIR, "ui_settings.json")


def resource_path(rel: str) -> str:
    base = getattr(sys, "_MEIPASS", APP_DIR)
    return os.path.join(base, rel)


def single_instance(name: str) -> bool:
    """Returns False if another instance already owns the named mutex."""
    global _instance_mutex
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        k32.CreateMutexW.restype = ctypes.c_void_p
        _instance_mutex = k32.CreateMutexW(None, False, name)
        return k32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


class DualNetApp:
    # ------------------------------------------------------------------ init
    def __init__(self, autostart=False):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

        preferences = display_settings(UI_STATE_PATH)
        self.theme = preferences["theme"]
        self.language = preferences["language"]
        self.compact = preferences["compact"]
        self.compact_topmost = preferences["compact_topmost"]
        self.colors = PALETTES[self.theme]
        self.root = tk.Tk()
        if autostart:
            self.root.withdraw()
        self.root.title("DualNet Splitter")
        self.root.configure(bg=self.colors["bg"])
        self.root.resizable(False, False)
        self.apply_window_priority()
        try:
            self.root.iconbitmap(resource_path(os.path.join("assets", "dualnet.ico")))
        except Exception:
            pass

        # Fit the full dashboard on laptop screens as well as high-DPI monitors.
        self.S = min(self.root.winfo_fpixels("1i") / 96.0,
                     (self.root.winfo_screenwidth() - 40) / W,
                     (self.root.winfo_screenheight() - 100) / H)
        fams = set(tkfont.families(self.root))
        self.f_disp = "Bahnschrift SemiBold" if "Bahnschrift SemiBold" in fams else "Segoe UI Semibold"
        self.f_cond = "Bahnschrift SemiBold Condensed" if "Bahnschrift SemiBold Condensed" in fams else self.f_disp
        self.f_light = "Bahnschrift Light" if "Bahnschrift Light" in fams else "Segoe UI Light"
        self.f_mono = "Consolas" if "Consolas" in fams else "Courier New"

        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        width, height = (COMPACT_W, COMPACT_H) if self.compact else (W, H)
        pw, ph = int(width * self.S), int(height * self.S)
        self.root.geometry(f"{pw}x{ph}+{(sw - pw) // 2}+{max(0, (sh - ph) // 2 - 20)}")

        self.cv = tk.Canvas(self.root, width=pw, height=ph, bg=self.colors["bg"], highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)

        # ---- backend state
        self.wifi = None
        self.eth = None
        self.wifi_ping: Optional[float] = None
        self.eth_ping: Optional[float] = None
        self.bandwidth = {"wifi": None, "lan": None}
        self.bandwidth_sampler = BandwidthSampler()
        self.wifi_hist = deque([None] * 60, maxlen=60)
        self.lan_hist = deque([None] * 60, maxlen=60)
        self.tun = TUNManager()
        self.proxy: Optional[DualNetProxy] = None
        self.proxy_loop = None
        self.autostart_on = False
        self.busy = {"tun": False, "proxy": False, "preset": False, "autostart": False,
                     "wifi": False, "lan": False}
        self.adapter_lock = threading.RLock()
        self.pending_preset = None
        self.status = self.tr('Siap. Monitoring berjalan senyap (native ICMP).')
        self.feedback_kind = "info"
        self.feedback_started = time.monotonic()
        self.adapter_scan_requested = False
        self.autostart_ready = False
        self.tun_requested = bool(self.tun.is_running)
        self.last_tun_retry = 0.0
        self.running = True
        self.wake = threading.Event()
        self.tray_icon = None
        self.tray_ready = threading.Event()
        self.tray_error = ""
        self.tray_starting = False
        self.tray_started_at = 0.0
        self.tray_warned = False
        self.tray_hide_pending = False
        self.tray_actions = queue.SimpleQueue()
        self.tour_step = None
        self.ids = {}
        self.cards = {}

        self.build_compact() if self.compact else self.build()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Escape>", lambda e: self.end_tour() if self.tour_step is not None else None)
        self.root.bind("<F1>", lambda e: self.show_guide())
        self.root.bind("<Right>", lambda e: self.advance_tour(1) if self.tour_step is not None else None)
        self.root.bind("<Left>", lambda e: self.advance_tour(-1) if self.tour_step else None)
        self.root.after(0, self.start_tray)
        self.root.after(100, self.poll_tray)

        threading.Thread(target=self.monitor_worker, daemon=True).start()
        threading.Thread(target=self.bandwidth_worker, daemon=True).start()
        threading.Thread(target=self._load_autostart_state, daemon=True).start()
        self.root.after(300, self.refresh_ui)
        if autostart:
            self.bg(self.autostart_worker, self.tr('Autostart: menunggu jaringan untuk mengaktifkan TUN...'))
        else:
            self.notify(self.tr('Siap. Klik Guide atau tekan F1 untuk tutorial terpandu.'))
            self.root.after(900, self.maybe_show_guide)

    # --------------------------------------------------------------- helpers
    def tr(self, message, **values):
        return translate(message, getattr(self, "language", "id"), **values)

    def set_display_preference(self, key, value):
        """Repaint on the Tk thread, keeping telemetry and services alive."""
        if key not in ("theme", "language", "compact", "compact_topmost"):
            return
        setattr(self, key, value)
        self.colors = PALETTES[self.theme]
        saved = save_settings(UI_STATE_PATH, theme=self.theme, language=self.language,
                              compact=self.compact, compact_topmost=self.compact_topmost)
        self.rebuild_display()
        if self.tray_icon:
            try:
                self.tray_icon.update_menu()
            except Exception:
                pass
        if not saved:
            self.notify(self.tr('Pengaturan tampilan belum tersimpan. Periksa izin folder aplikasi.'), "error")

    def apply_window_priority(self):
        self.root.attributes("-topmost", bool(getattr(self, "compact", False)
                                             and getattr(self, "compact_topmost", False)))

    def rebuild_display(self):
        self.apply_window_priority()
        width, height = (COMPACT_W, COMPACT_H) if self.compact else (W, H)
        pw, ph = round(width * self.S), round(height * self.S)
        # Keep the current location where possible; fit expanded windows onscreen.
        x = max(0, min(self.root.winfo_x(), self.root.winfo_screenwidth() - pw))
        y = max(0, min(self.root.winfo_y(), self.root.winfo_screenheight() - ph - 50))
        self.root.geometry(f"{pw}x{ph}+{x}+{y}")
        self.root.configure(bg=self.colors["bg"])
        self.cv.delete("all")
        self.cv.configure(width=pw, height=ph, bg=self.colors["bg"], cursor="")
        self.ids, self.cards = {}, {}
        self.build_compact() if self.compact else self.build()
        self.refresh_ui(schedule=False)
        if self.tour_step is not None and not self.compact:
            self.draw_tour()

    def build_display_controls(self, x, y):
        controls = [
            ("theme", self.tr('Tema: Gelap') if self.theme == "dark" else self.tr('Tema: Terang'), 126,
             lambda: self.set_display_preference("theme", "light" if self.theme == "dark" else "dark")),
            ("language", self.tr('Bahasa: ID') if self.language == "id" else "Language: EN", 120,
             lambda: self.set_display_preference("language", "en" if self.language == "id" else "id")),
            ("compact", self.tr('↗ Lengkap') if self.compact else "↙ Compact", 110,
             lambda: self.set_display_preference("compact", not self.compact)),
        ]
        for key, label, width, callback in controls:
            tag = f"display_{key}"
            self.rect(x, y - 12, x + width, y + 12, fill=self.colors["panel"],
                      outline=self.colors["line"], tags=(tag,))
            self.text(x + width / 2, y, self.tr(label), self.F(self.f_mono, 11),
                      anchor="center", tags=(tag,))
            self.clickable(tag, callback)
            x += width + 8

    def build_compact(self):
        self.rect(0, 0, COMPACT_W, 4, fill=self.colors["blue"])
        self.text(16, 17, "DUALNET / MONITOR", self.F(self.f_disp, 14))
        pin_tag = "display_compact_topmost"
        self.rect(170, 7, 340, 27, fill=self.colors["blue"] if self.compact_topmost else self.colors["panel"],
                  outline=self.colors["blue"] if self.compact_topmost else self.colors["line"], tags=(pin_tag,))
        self.text(255, 17, self.tr("✓ Selalu di atas" if self.compact_topmost else "Selalu di atas: OFF"),
                  self.F(self.f_mono, 10), anchor="center",
                  fill=self.colors["on_accent"] if self.compact_topmost else self.colors["ink"], tags=(pin_tag,))
        self.clickable(pin_tag, lambda: self.set_display_preference("compact_topmost", not self.compact_topmost))
        self.ids["clock"] = self.text(464, 17, "--:--:--", self.F(self.f_mono, 11), anchor="e")
        self.build_display_controls(16, 42)
        self.text(442, 42, "Tray", self.F(self.f_mono, 11), anchor="center", tags=("compact_tray",))
        self.rect(418, 30, 466, 54, outline=self.colors["line"], tags=("compact_tray",))
        self.clickable("compact_tray", self.to_tray)
        for key, x, title in (("wifi", 16, "WI-FI"), ("lan", 248, "LAN")):
            color = self.colors["blue"] if key == "wifi" else self.colors["ink"]
            self.rect(x, 62, x + 216, 136, fill=self.colors["panel"], outline=self.colors["line"])
            self.text(x + 10, 75, title, self.F(self.f_mono, 11, "bold"), fill=color)
            self.ids[f"{key}_tag"] = self.text(x + 206, 75, "", self.F(self.f_mono, 9), anchor="e")
            self.ids[f"{key}_big"] = self.text(x + 10, 101, "--", self.F(self.f_cond, 32), fill=color)
            self.ids[f"{key}_unit"] = self.text(x + 80, 108, "ms", self.F(self.f_disp, 11), fill=color)
            self.ids[f"{key}_bandwidth"] = self.text(x + 10, 125, "↓ -- B/s  ↑ -- B/s",
                                                      self.F(self.f_mono, 10), fill=color)
            self.ids[f"{key}_ip"] = self.text(x + 102, 103, "", self.F(self.f_mono, 9), fill=self.colors["dim"])
        self.graph = (16, 144, 232, 174)
        self.rect(*self.graph, fill=self.colors["panel"], outline=self.colors["line"])
        self.grid_lbl = []
        self.line(16, 159, 232, 159, fill=self.colors["faint"], dash=(2, 3))
        for key, color in (("wifi", "blue"), ("lan", "lime_dk")):
            self.ids[f"{key}_line"] = self.cv.create_line(0, 0, 1, 1, fill=self.colors[color], width=2 * self.S)
        self.ids["service_status"] = self.text(464, 149, "", self.F(self.f_mono, 10), anchor="e")
        self.ids["status"] = self.text(248, 168, "", self.F(self.f_mono, 10), width=216 * self.S)

    def refresh_compact(self):
        for key, adapter, ip, ping in (("wifi", self.wifi, self.wifi_ip(), self.wifi_ping),
                                        ("lan", self.eth, self.eth_ip(), self.eth_ping)):
            self.set_big(key, ping if ip else None, bool(ip),
                         self.colors["blue"] if key == "wifi" else self.colors["ink"])
            rates = self.bandwidth[key]
            label = (f"↓ {format_rate(rates[0])}  ↑ {format_rate(rates[1])}"
                     if ip and rates is not None else "↓ -- B/s  ↑ -- B/s")
            self.cv.itemconfig(self.ids[f"{key}_bandwidth"], text=label)
            state = ip or self.tr('Tidak ditemukan' if not adapter else
                                  'Nonaktif' if not adapter_enabled(adapter) else 'Belum tersambung')
            self.cv.itemconfig(self.ids[f"{key}_ip"], text=state)
        tun = self.tr('PROSES' if self.busy["tun"] else 'AKTIF' if self.tun.is_running else "OFF")
        proxy = self.tr('PROSES' if self.busy["proxy"] else 'AKTIF' if self.proxy and self.proxy.is_running else "OFF")
        self.cv.itemconfig(self.ids["service_status"], text=f"TUN {tun}  ·  PROXY {proxy}")
        self.draw_graph()
        color = self.colors["red"] if self.feedback_kind == "error" else self.colors["dim"]
        # One line, with ellipsis, keeps long diagnostics inside the small window.
        message = self.tr(self.status).replace("\n", " ")
        font = self.F(self.f_mono, 10)
        if self.measure(font, message) > 216:
            while message and self.measure(font, message + "…") > 216:
                message = message[:-1]
            message += "…"
        self.cv.itemconfig(self.ids["status"], text=message, fill=color)

    def s(self, *v):
        return [x * self.S for x in v]

    def F(self, family, px, weight="normal"):
        return (family, -max(1, int(round(px * self.S))), weight)

    def text(self, x, y, txt, font, fill=None, anchor="w", **kw):
        return self.cv.create_text(*self.s(x, y), text=self.tr(txt), font=font, fill=fill or self.colors["ink"], anchor=anchor, **kw)

    def rect(self, x1, y1, x2, y2, fill="", outline="", width=1, **kw):
        return self.cv.create_rectangle(*self.s(x1, y1, x2, y2), fill=fill, outline=outline,
                                        width=max(1, width * self.S) if outline else 0, **kw)

    def line(self, *pts, fill=None, width=1, **kw):
        return self.cv.create_line(*self.s(*pts), fill=fill or self.colors["line"], width=width * self.S, **kw)

    def measure(self, font, txt):
        return tkfont.Font(root=self.root, font=font).measure(txt) / self.S

    def clickable(self, tag, cb):
        self.cv.tag_bind(tag, "<Button-1>", lambda e: cb())
        self.cv.tag_bind(tag, "<Enter>", lambda e: self.cv.config(cursor="hand2"))
        self.cv.tag_bind(tag, "<Leave>", lambda e: self.cv.config(cursor=""))

    # ----------------------------------------------------------------- build
    def build(self):
        cv = self.cv
        mono9, mono10 = self.F(self.f_mono, 11), self.F(self.f_mono, 12)

        # ---- background decoration (behind everything)
        for (a, b, c) in [((60, 360), (230, 190), (400, 360)), ((60, 470), (230, 300), (400, 470))]:
            self.line(*a, *b, *c, fill=self.colors["deco"], width=30, capstyle="projecting", joinstyle="miter")
        for x in (770, 905):
            self.line(x, 0, x, 75, fill=self.colors["faint"])
        self.line(700, 46, 950, 46, fill=self.colors["faint"])
        for x in (770, 905):
            self.rect(x - 4, 42, x + 4, 50, fill=self.colors["bg"], outline=self.colors["ink"], width=1.4)
        self.rect(901, 38, 909, 46, fill=self.colors["blue"])

        # ---- top bar
        self.rect(0, 0, 300, 7, fill=self.colors["ink"])
        self.line(300, 3, 318, 13, W, 13, fill=self.colors["ink"], width=1.2)
        f_title = self.F(self.f_disp, 34)
        self.text(40, 50, "DUALNET", f_title)
        tw = self.measure(f_title, "DUALNET")
        self.text(40 + tw + 10, 50, "//SPLITTER", self.F(self.f_light, 34), fill=self.colors["blue"])
        self.text(42, 78, "[ WI-FI LOW LATENCY  ⇄  LAN HIGH THROUGHPUT ]   ROUTER v2", mono9, fill=self.colors["dim"])

        self.ids["clock"] = self.text(1080, 40, "--:--:--", self.F(self.f_mono, 17), anchor="e")
        admin = is_admin()
        self.text(1080, 62, "[ ADMINISTRATOR ]" if admin else self.tr('[ USER MODE - TUN BUTUH ADMIN ]'),
                  mono9, fill=self.colors["blue"] if admin else self.colors["red"], anchor="e")

        self.build_display_controls(640, 83)

        # ---- vertical tag + index rail
        self.text(20, 320, "— DUALNET ROUTER —", self.F(self.f_mono, 12, "bold"), fill=self.colors["blue"],
                  anchor="center", angle=90)
        self.line(52, 118, 52, 530, fill=self.colors["line"])
        rail_labels = ["WI-FI", "LAN", "TUN", "PROXY"]
        self.rail = {}
        for i, lbl in enumerate(rail_labels):
            cy = 112 + i * 108 + 43
            self.line(52, cy, 64, cy, fill=self.colors["line"])
            self.text(40, cy - 22, f"#0{i + 1}", self.F(self.f_mono, 10), fill=self.colors["dim"])
            t = self.text(70, cy - 5, lbl, self.F(self.f_disp, 18), fill=self.colors["ink"])
            st = self.text(70, cy + 15, "□ □ □", self.F(self.f_mono, 10), fill=self.colors["dim"])
            self.text(150, cy, "▸", self.F(self.f_mono, 13), fill=self.colors["ink"], anchor="center")
            self.rail[i] = (t, st)

        # ---- main cards
        specs = [("wifi", "Wi-Fi", False), ("lan", "LAN", False),
                 ("tun", "TUN Mode", True), ("proxy", "Proxy", True)]
        for i, (key, title, click) in enumerate(specs):
            self.build_card(i, key, title, click)

        # ---- right panel: latency
        self.text(1080, 104, "[ Latency Monitor ]", mono10, anchor="e")
        self.text(640, 126, "[01] WI-FI PING", mono9, fill=self.colors["dim"])
        self.text(870, 126, "[02] LAN PING", mono9, fill=self.colors["dim"])
        f_big = self.F(self.f_cond, 84)
        self.ids["wifi_big"] = self.text(636, 182, "--", f_big, fill=self.colors["blue"])
        self.ids["wifi_unit"] = self.text(700, 204, "ms", self.F(self.f_disp, 16), fill=self.colors["blue"])
        self.ids["lan_big"] = self.text(866, 182, "--", f_big, fill=self.colors["ink"])
        self.ids["lan_unit"] = self.text(930, 204, "ms", self.F(self.f_disp, 16), fill=self.colors["ink"])
        self.ids["wifi_tag"] = self.text(842, 126, "○ WAIT", self.F(self.f_mono, 9), fill=self.colors["dim"], anchor="e")
        self.ids["lan_tag"] = self.text(1080, 126, "○ WAIT", self.F(self.f_mono, 9), fill=self.colors["dim"], anchor="e")
        for key, x, color in (("wifi", 640, self.colors["blue"]), ("lan", 870, self.colors["ink"])):
            self.ids[f"{key}_bandwidth"] = self.text(
                x, 230, "↓ -- B/s   ↑ -- B/s", self.F(self.f_mono, 11), fill=color)

        # divider with x marks
        self.line(640, 246, 1080, 246, fill=self.colors["ink"], width=2)
        for k, x in enumerate((990, 1010, 1030, 1050)):
            self.text(x, 258, "×", mono10, fill=self.colors["blue"] if k == 2 else self.colors["ink"], anchor="center")
        self.text(640, 258, "ICMP ▸ 1.1.1.1 / fallback 8.8.8.8", self.F(self.f_mono, 10), fill=self.colors["dim"])

        # graph
        gx1, gy1, gx2, gy2 = 690, 274, 1080, 400
        self.graph = (gx1, gy1, gx2, gy2)
        self.rect(gx1, gy1, gx2, gy2, fill=self.colors["panel"], outline=self.colors["line"])
        self.grid_lbl = []
        for k, (name, fy) in enumerate((("HIGH", 0.0), ("MID", 0.5), ("LOW", 1.0))):
            y = gy1 + 8 + (gy2 - gy1 - 16) * fy
            self.line(gx1, y, gx2, y, fill=self.colors["faint"], dash=(2, 3))
            self.cv.create_oval(*self.s(652, y - 12, 676, y + 12), fill=self.colors["faint"], outline="")
            self.grid_lbl.append(self.text(664, y, "0", self.F(self.f_mono, 10), anchor="center"))
        self.ids["lan_line"] = self.cv.create_line(0, 0, 1, 1, fill=self.colors["lime_dk"], width=2 * self.S)
        self.ids["wifi_line"] = self.cv.create_line(0, 0, 1, 1, fill=self.colors["blue"], width=2.2 * self.S)
        self.rect(690, 409, 700, 419, fill=self.colors["blue"])
        self.text(706, 414, "WI-FI", self.F(self.f_mono, 10))
        self.rect(760, 409, 770, 419, fill=self.colors["lime_dk"])
        self.text(776, 414, "LAN", self.F(self.f_mono, 10))
        self.text(1080, 414, "60s WINDOW", self.F(self.f_mono, 10), fill=self.colors["dim"], anchor="e")

        # presets
        self.text(640, 440, "[ Metric Preset ]", mono9, fill=self.colors["dim"])
        self.ids["active_preset"] = self.text(1080, 440, self.tr('METRIC: MEMUAT...'), mono9, fill=self.colors["dim"], anchor="e")
        presets = [
            ("gaming", "GAMING", "[G]", "WIFI 10 / LAN 50", self.colors["blue"], self.colors["on_accent"], self.colors["blue"]),
            ("download", "DOWNLOAD", "[D]", "LAN 10 / WIFI 50", self.colors["lime"], self.colors["on_lime"], self.colors["lime"]),
            ("reset", "AUTO", "[R]", "RESET METRIC", self.colors["blue_lt"], self.colors["ink"], self.colors["blue"]),
        ]
        bw = (440 - 2 * 12) / 3
        self.preset_buttons = {}
        for k, (key, title, tag, sub, fill, fg, outl) in enumerate(presets):
            x1 = 640 + k * (bw + 12)
            x2, y1, y2 = x1 + bw, 452, 512
            t = f"btn_{key}"
            r = self.rect(x1, y1, x2, y2, fill=self.colors["panel"], outline=self.colors["line"], width=1.4, tags=(t,))
            labels = [self.text(x1 + 10, y1 + 12, tag, self.F(self.f_mono, 10), tags=(t,)),
                      self.text(x1 + 10, y1 + 34, title, self.F(self.f_disp, 19), tags=(t,)),
                      self.text(x1 + 10, y2 - 10, sub, self.F(self.f_mono, 9), tags=(t,))]
            marker = self.text(x2 - 9, y1 + 12, "", self.F(self.f_mono, 9, "bold"), anchor="e", tags=(t,))
            self.preset_buttons[key] = {"box": r, "labels": labels, "marker": marker,
                                        "fill": fill, "fg": fg, "outline": outl}
            self.ids[t] = (r, fill)
            self.clickable(t, lambda kk=key: self.apply_preset(kk))

        self.ids["proxy_stats"] = self.text(640, 532, "PROXY ▸ OFF", self.F(self.f_mono, 11), fill=self.colors["dim"])

        # ---- link quality bar
        self.text(40, 566, "[ Link Quality ]", mono10)
        self.rect(166, 561, 174, 569, fill=self.colors["blue"])
        self.ids["q_pct"] = self.text(1080, 566, "0%", self.F(self.f_mono, 12), anchor="e")
        self.rect(40, 578, 1080, 594, fill=self.colors["faint"], outline=self.colors["line"])
        self.ids["q_bar"] = self.rect(42, 580, 43, 592, fill=self.colors["blue"])
        for x in range(46, 1078, 7):
            self.line(x, 581, x, 591, fill=self.colors["deco"])
        self.cv.tag_raise(self.ids["q_bar"])

        # ---- bottom nav
        self.text(40, 630, "|— DUALNET —|", self.F(self.f_mono, 10), fill=self.colors["dim"])
        nav = [("autostart", "Autostart"), ("rules", "Edit Rules"), ("steam", "Steam via LAN"),
               ("guide", "Guide"), ("refresh", "Refresh"), ("tray", "Tray")]
        self.nav_box = {}
        x = 170
        for key, label in nav:
            t = f"nav_{key}"
            box = self.rect(x, 624, x + 12, 636, fill=self.colors["bg"], outline=self.colors["ink"], width=1.4, tags=(t,))
            chk = self.line(x + 2, 630, x + 5, 633, x + 10, 626, fill=self.colors["blue"], width=2.2,
                            state="hidden", tags=(t,))
            self.text(x + 20, 630, label, self.F(self.f_mono, 12), tags=(t,))
            hit = self.rect(x - 4, 618, x + 24 + self.measure(self.F(self.f_mono, 12), label), 642,
                            fill="", outline="", tags=(t,))
            self.cv.tag_lower(hit)
            self.nav_box[key] = (box, chk)
            self.clickable(t, lambda kk=key: self.nav_action(kk))
            x += 40 + self.measure(self.F(self.f_mono, 12), label) + 14

        self.text(1018, 630, "×", self.F(self.f_disp, 30), anchor="center", tags=("btn_quit",))
        self.rect(1012, 618, 1024, 642, fill="", outline="", tags=("btn_quit",))
        self.clickable("btn_quit", self.quit_app)
        self.rect(1052, 620, 1074, 642, fill="", outline=self.colors["blue"], width=2.4, tags=("btn_min",))
        self.rect(1058, 626, 1068, 636, fill=self.colors["bg"], outline="", tags=("btn_min",))
        self.clickable("btn_min", self.to_tray)

        self.ids["feedback_bg"] = self.rect(40, 652, 1080, 692, fill=self.colors["panel"], outline=self.colors["line"])
        self.ids["feedback_accent"] = self.rect(40, 652, 44, 692, fill=self.colors["blue"])
        self.ids["feedback_symbol"] = self.text(62, 672, "i", self.F(self.f_mono, 16, "bold"), fill=self.colors["blue"], anchor="center")
        self.ids["status"] = self.text(82, 672, "", self.F(self.f_mono, 11), width=980 * self.S)

    def build_card(self, i, key, title, click):
        x1, x2 = 170, 600
        y1 = 112 + i * 108
        y2 = y1 + 86
        cy = (y1 + y2) / 2
        t = f"card_{key}"
        d = {}
        self.text(x1 + 2, y1 - 5, f"[0{i + 1}]", self.F(self.f_mono, 10), fill=self.colors["dim"], anchor="sw")
        d["box"] = self.rect(x1, y1, x2, y2, fill=self.colors["bg"], outline=self.colors["blue"], width=1, tags=(t,))
        d["checker"] = []
        for r in range(0, 86, 5):
            for c in range(2):
                if (r // 5 + c) % 2 == 0:
                    d["checker"].append(self.rect(x1 + 1 + c * 4, y1 + r + 1, x1 + 4 + c * 4, y1 + r + 4,
                                                  fill=self.colors["blue"], tags=(t,)))
        d["cb"] = self.rect(x1 + 26, cy - 11, x1 + 48, cy + 11, fill="", outline=self.colors["ink"], width=2.2, tags=(t,))
        d["chk"] = self.line(x1 + 30, cy, x1 + 35, cy + 6, x1 + 45, cy - 6, fill=self.colors["ink"], width=3,
                             state="hidden", tags=(t,))
        d["title"] = self.text(x1 + 64, cy - 8, title, self.F(self.f_light, 36), tags=(t,))
        d["sub"] = self.text(x1 + 66, cy + 24, "", self.F(self.f_mono, 10), fill=self.colors["dim"], tags=(t,))
        d["num"] = self.text(x2 - 34, cy + 2, f"0{i + 1}", self.F(self.f_cond, 96), fill=self.colors["blue"],
                             anchor="e", tags=(t,))
        d["arrow"] = self.text(x2 - 8, y1 + 11, "↗", self.F(self.f_mono, 11), anchor="e", tags=(t,))
        d["x"] = self.text(x2 - 16, cy, "×" if not click else "⏻", self.F(self.f_mono, 15), anchor="center", tags=(t,))
        d["state"] = self.text(x2 - 8, y2 - 10, "", self.F(self.f_disp, 11), fill=self.colors["blue"], anchor="e", tags=(t,))
        self.line(x2 - 36, y2 + 6, x2 - 8, y2 + 6, fill=self.colors["ink"], width=2, dash=(4, 2))
        if i < 3:
            self.line(380, y2 + 8, 385, y2 + 14, 390, y2 + 8, fill=self.colors["ink"], width=1.4)
        if click:
            self.clickable(t, lambda: self.toggle(key))
            self.cv.tag_bind(t, "<Enter>", lambda e: self.cv.itemconfig(d["box"], width=2.6 * self.S), add="+")
            self.cv.tag_bind(t, "<Leave>", lambda e: self.cv.itemconfig(d["box"], width=1 * self.S), add="+")
        elif key in ("wifi", "lan"):
            # Explicit controls keep clicks on adapter information harmless.
            toggle_tag = f"adapter_toggle_{key}"
            self.cv.addtag_withtag(toggle_tag, d["cb"])
            self.cv.addtag_withtag(toggle_tag, d["chk"])
            d["adapter_button"] = self.rect(365, cy - 20, 480, cy + 6, fill=self.colors["panel"],
                                              outline=self.colors["blue"], tags=(toggle_tag,))
            d["adapter_action"] = self.text(422.5, cy - 7, self.tr('MEMUAT...'), self.F(self.f_mono, 11),
                                            anchor="center", tags=(toggle_tag,))
            self.clickable(toggle_tag, lambda: self.toggle_adapter(key))
            self.cv.itemconfig(d["x"], state="hidden")
        self.cards[key] = d

    def set_card(self, key, active, sub, state):
        d = self.cards[key]
        cv = self.cv
        if active:
            cv.itemconfig(d["box"], fill=self.colors["blue"], outline=self.colors["blue"])
            for it in d["checker"]:
                cv.itemconfig(it, state="hidden")
            fg, num, dim = self.colors["on_accent"], self.colors["on_accent"], self.colors["accent_muted"]
            cv.itemconfig(d["chk"], state="normal", fill=self.colors["on_accent"])
        else:
            cv.itemconfig(d["box"], fill=self.colors["bg"], outline=self.colors["blue"])
            for it in d["checker"]:
                cv.itemconfig(it, state="normal")
            fg, num, dim = self.colors["ink"], self.colors["blue"], self.colors["dim"]
            cv.itemconfig(d["chk"], state="hidden")
        cv.itemconfig(d["cb"], outline=fg)
        cv.itemconfig(d["title"], fill=fg)
        cv.itemconfig(d["sub"], text=sub, fill=dim)
        cv.itemconfig(d["num"], fill=num)
        cv.itemconfig(d["arrow"], fill=fg)
        cv.itemconfig(d["x"], fill=fg)
        cv.itemconfig(d["state"], text=state, fill=self.colors["on_accent"] if active else self.colors["blue"])

    def set_rail(self, i, on, label_on="ONLINE"):
        t, st = self.rail[i]
        self.cv.itemconfig(t, fill=self.colors["blue"] if on else self.colors["ink"])
        self.cv.itemconfig(st, text="■ ■ ■  " + label_on if on else "□ □ □  OFF",
                           fill=self.colors["blue"] if on else self.colors["dim"])

    # --------------------------------------------------------------- backend
    def scan_adapters(self):
        with self.adapter_lock:
            self.wifi, self.eth = find_primary_adapters()
        return self.wifi, self.eth

    def toggle_adapter(self, key):
        if any(self.busy[k] for k in ("wifi", "lan", "preset")):
            self.notify(self.tr('Perubahan jaringan masih diproses. Tunggu hingga selesai.'), "progress")
            return
        adapter = self.wifi if key == "wifi" else self.eth
        label = "Wi-Fi" if key == "wifi" else "LAN"
        if not adapter or not adapter.get("index"):
            self.notify(self.tr('Adapter {v0} belum ditemukan. Klik Refresh.', v0=label), "error")
            return
        enabled = not adapter_enabled(adapter)
        self.busy[key] = True

        def work():
            try:
                with self.adapter_lock:
                    proc = set_adapter_enabled(adapter["index"], enabled)
                    if proc.returncode != 0:
                        self.notify(self.tr('Gagal mengubah {v0}: {v1}', v0=label,
                                            v1=proc.stderr.strip() or self.tr('izin Administrator dibatalkan.')), "error")
                        return
                    self.scan_adapters()
                    observed = self.wifi if key == "wifi" else self.eth
                    if not observed or observed.get("index") != adapter["index"] or adapter_enabled(observed) != enabled:
                        self.notify(self.tr('Status {v0} belum terkonfirmasi. Klik Refresh untuk memeriksa ulang.', v0=label), "error")
                    elif enabled:
                        self.notify(self.tr('Adapter {v0} diaktifkan.{v1}', v0=label,
                                            v1=self.tr(' Menunggu koneksi dan alamat IP.') if observed.get("state") != "Up" or not observed.get("ip") else self.tr(' Koneksi tersedia.')), "success")
                    else:
                        if key == "wifi":
                            self.wifi_ping = None
                        else:
                            self.eth_ping = None
                        self.notify(self.tr('Adapter {v0} dinonaktifkan. Klik AKTIFKAN untuk menyalakan kembali.', v0=label), "success")
            finally:
                self.busy[key] = False
                self.wake.set()
        self.bg(work, self.tr('{v0} adapter {v1}...', v0=self.tr('Mengaktifkan') if enabled else self.tr('Menonaktifkan'), v1=label))

    def eth_ip(self) -> Optional[str]:
        if self.eth and self.eth.get("state") == "Up":
            ip = self.eth.get("ip") or ""
            if ip and not ip.startswith(("169.254", "127.")):
                return ip
        return None

    def wifi_ip(self) -> Optional[str]:
        if self.wifi and self.wifi.get("state") == "Up":
            ip = self.wifi.get("ip") or ""
            if ip and not ip.startswith("169.254"):
                return ip
        return None

    def monitor_worker(self):
        last_adapters = 0.0
        while self.running:
            if time.time() - last_adapters >= 10:
                try:
                    self.scan_adapters()
                    if self.adapter_scan_requested:
                        self.adapter_scan_requested = False
                        self.notify(self.tr('Refresh selesai. Adapter dan alamat IP sudah diperbarui.'), "success")
                except Exception:
                    if self.adapter_scan_requested:
                        self.adapter_scan_requested = False
                        self.notify(self.tr('Refresh gagal. Coba lagi setelah beberapa saat.'), "error")
                last_adapters = time.time()
            w_ip, e_ip = self.wifi_ip(), self.eth_ip()
            try:
                wp, ep = measure_both_pings(w_ip, e_ip)
            except Exception:
                wp, ep = None, None
            self.wifi_ping, self.eth_ping = wp, ep
            self.wifi_hist.append(wp if w_ip else None)
            self.lan_hist.append(ep if e_ip else None)
            if self.proxy:
                try:
                    self.proxy.stats.update_speed()
                except Exception:
                    pass
            if not self.busy["tun"]:
                try:
                    healthy = self.tun.check_health()
                    if (self.autostart_ready and self.tun_requested and not healthy and self.wifi_ip()
                            and time.monotonic() - self.last_tun_retry >= 15):
                        self.last_tun_retry = time.monotonic()
                        self.toggle("tun")
                except Exception:
                    pass
            self.wake.wait(1.0)
            if self.wake.is_set():
                self.wake.clear()
                last_adapters = 0

    def bandwidth_worker(self):
        while self.running:
            started = time.monotonic()
            self.bandwidth = {
                "wifi": self.bandwidth_sampler.sample("wifi", self.wifi),
                "lan": self.bandwidth_sampler.sample("lan", self.eth),
            }
            # Separate from ping timeouts and the slower adapter scan.
            time.sleep(max(0.05, 1.0 - (time.monotonic() - started)))

    def autostart_worker(self):
        deadline = time.monotonic() + 90
        while self.running and not self.wifi_ip() and time.monotonic() < deadline:
            time.sleep(1)
        if not self.running:
            return
        if self.tun.check_health():
            self.tun_requested = True
            self.autostart_ready = True
            self.notify(self.tr('Autostart siap. TUN sudah aktif; DualNet tersedia di tray.'), "success")
        elif self.wifi_ip():
            self.autostart_ready = True
            self.toggle("tun")
        else:
            self.notify(self.tr('Autostart: Wi-Fi belum siap. Sambungkan Wi-Fi lalu aktifkan TUN.'), "error")

    def notify(self, message, kind="info"):
        """Workers publish feedback; only the Tk thread paints it."""
        self.status = message
        self.feedback_kind = kind
        self.feedback_started = time.monotonic()

    def _load_autostart_state(self):
        try:
            self.autostart_on = is_autostart_installed()
        except Exception:
            self.autostart_on = False

    def bg(self, fn, busy_msg=None):
        if busy_msg:
            self.notify(busy_msg, "progress")

        def run():
            try:
                fn()
            except Exception as e:
                self.notify(self.tr('Gagal: {v0}', v0=e), "error")
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------- UI update
    def refresh_ui(self, schedule=True):
        if not self.running:
            return
        cv = self.cv
        cv.itemconfig(self.ids["clock"], text=time.strftime("%H:%M:%S"))
        if _show_event and ctypes.windll.kernel32.WaitForSingleObject(ctypes.c_void_p(_show_event), 0) == 0:
            self.show_window()

        if self.compact:
            self.refresh_compact()
            if schedule:
                self.root.after(250, self.refresh_ui)
            return

        # Adapter connection state and power controls.
        w_ip = self.wifi_ip()
        self.update_adapter_card("wifi", self.wifi, w_ip)
        self.set_rail(0, bool(w_ip))
        self.set_big("wifi", self.wifi_ping, bool(w_ip), self.colors["blue"])

        # LAN
        e_ip = self.eth_ip()
        self.update_adapter_card("lan", self.eth, e_ip)
        self.set_rail(1, bool(e_ip))
        self.set_big("lan", self.eth_ping, bool(e_ip), self.colors["ink"])
        self.update_preset_ui()
        for key, online in (("wifi", bool(w_ip)), ("lan", bool(e_ip))):
            rates = self.bandwidth[key]
            if online and rates is not None:
                label = f"↓ {format_rate(rates[0])}  ↑ {format_rate(rates[1])}"
            else:
                label = "↓ -- B/s  ↑ -- B/s"
            cv.itemconfig(self.ids[f"{key}_bandwidth"], text=label,
                          fill=(self.colors["blue"] if key == "wifi" else self.colors["ink"]) if online else self.colors["dim"])

        # TUN
        tun_on = bool(self.tun and self.tun.is_running)
        if self.busy["tun"]:
            sub, state = self.tr('MEMPROSES...'), "WAIT"
        elif tun_on:
            dn = len(self.tun.rules.get("download_processes", []))
            gm = len(self.tun.rules.get("gaming_processes", []))
            sub, state = f"{dn} APPS → LAN  ·  {gm} GAMES → WI-FI  ·  WINTUN", "ACTIVE"
        else:
            sub, state = self.tr('KLIK UNTUK AKTIFKAN ZERO-CONFIG SPLIT'), "OFF"
        self.set_card("tun", tun_on, sub, state)
        self.set_rail(2, tun_on, "ACTIVE")

        # Proxy
        p_on = bool(self.proxy and self.proxy.is_running)
        if self.busy["proxy"]:
            sub, state = self.tr('MEMPROSES...'), "WAIT"
        elif p_on:
            sub, state = "HTTP 127.0.0.1:8080  ·  SOCKS5 :1080", "ACTIVE"
        else:
            sub, state = self.tr('KLIK UNTUK AKTIFKAN PROXY LEGACY'), "OFF"
        self.set_card("proxy", p_on, sub, state)
        self.set_rail(3, p_on, "ACTIVE")
        if p_on:
            st = self.proxy.stats
            bound = self.proxy.get_effective_outbound_ip() or "fallback"
            cv.itemconfig(self.ids["proxy_stats"], fill=self.colors["ink"], text=(
                f"PROXY ▸ ↓ {st.current_download_speed / 1048576:.2f} MB/s   ↑ {st.current_upload_speed / 1048576:.2f} MB/s"
                f"   Σ {st.bytes_recv / 1048576:.1f} MB   CONN {st.active_conns}   OUT {bound}"))
        else:
            cv.itemconfig(self.ids["proxy_stats"], fill=self.colors["dim"], text="PROXY ▸ OFF")

        # Graph + quality
        self.draw_graph()
        q = 0.0
        if self.wifi_ping is not None:
            q = max(5.0, min(100.0, 100 - (self.wifi_ping - 15) * 0.7))
        x2 = 42 + (1078 - 42) * q / 100
        cv.coords(self.ids["q_bar"], *self.s(42, 580, max(43, x2), 592))
        cv.itemconfig(self.ids["q_pct"], text=f"{q:.0f}%")

        # Nav autostart
        box, chk = self.nav_box["autostart"]
        cv.itemconfig(chk, state="normal" if self.autostart_on else "hidden")
        feedback_colors = {"info": self.colors["blue"], "progress": self.colors["blue"],
                           "success": self.colors["lime_dk"], "error": self.colors["red"]}
        color = feedback_colors.get(self.feedback_kind, self.colors["blue"])
        symbol = {"info": "i", "success": "✓", "error": "!"}.get(self.feedback_kind, "…")
        if self.feedback_kind == "progress":
            symbol = "◐◓◑◒"[int(time.monotonic() * 4) % 4]
        cv.itemconfig(self.ids["feedback_accent"], fill=color)
        cv.itemconfig(self.ids["feedback_bg"], outline=color)
        cv.itemconfig(self.ids["feedback_bg"], width=(2 if time.monotonic() - self.feedback_started < 1.2 else 1) * self.S)
        cv.itemconfig(self.ids["feedback_symbol"], text=symbol, fill=color)
        cv.itemconfig(self.ids["status"], text=self.tr(self.status))

        if schedule:
            self.root.after(250, self.refresh_ui)

    def update_adapter_card(self, key, adapter, ip):
        enabled = adapter_enabled(adapter)
        if not adapter:
            sub, state = self.tr('ADAPTER TIDAK DITEMUKAN · KLIK REFRESH'), self.tr('TIDAK ADA')
        elif self.busy[key]:
            sub, state = self.tr('MENGUBAH STATUS ADAPTER...'), "WAIT"
        elif not enabled:
            sub, state = self.tr('ADAPTER DINONAKTIFKAN'), self.tr('NONAKTIF')
        elif ip:
            sub, state = f"IP {ip}  ·  METRIC {adapter.get('metric', '?')}", "ONLINE"
        elif adapter.get("state") == "Up":
            sub, state = self.tr('MENUNGGU ALAMAT IP (DHCP)'), "NO IP"
        else:
            sub = self.tr('WI-FI BELUM TERSAMBUNG') if key == "wifi" else self.tr('KABEL BELUM DICOLOK (UNPLUGGED)')
            state = "NO LINK"
        self.set_card(key, bool(ip), sub, state)
        d = self.cards[key]
        self.cv.itemconfig(d["cb"], fill=self.colors["blue"] if ip else self.colors["bg"])
        self.cv.itemconfig(d["chk"], state="normal" if enabled else "hidden", fill=self.colors["on_accent"] if ip else self.colors["blue"])
        action = self.tr('PROSES...') if self.busy[key] else (self.tr('⏻ MATIKAN') if enabled else self.tr('⏻ AKTIFKAN'))
        if not adapter:
            action = self.tr('TIDAK ADA')
        self.cv.itemconfig(d["adapter_action"], text=action, fill=self.colors["blue"] if adapter else self.colors["dim"])
        self.cv.itemconfig(d["adapter_button"], outline=self.colors["blue"] if adapter else self.colors["line"])

    def update_preset_ui(self):
        current = detect_metric_preset(self.wifi, self.eth)
        labels = {"gaming": "GAMING", "download": "DOWNLOAD", "reset": "AUTO", "custom": "CUSTOM"}
        if self.pending_preset:
            header = self.tr('MENERAPKAN: ') + labels[self.pending_preset]
        elif current == "unknown":
            header = self.tr('METRIC: BELUM TERBACA')
        else:
            header = self.tr('AKTIF: ') + labels[current]
        self.cv.itemconfig(self.ids["active_preset"], text=header,
                          fill=self.colors["blue"] if current in self.preset_buttons or self.pending_preset else self.colors["dim"])
        for key, button in self.preset_buttons.items():
            active, pending = key == current, key == self.pending_preset
            fg = button["fg"] if active else self.colors["ink"]
            self.cv.itemconfig(button["box"], fill=button["fill"] if active else self.colors["panel"],
                              outline=button["outline"] if active or pending else self.colors["line"],
                              width=(3 if active or pending else 1) * self.S, dash=(3, 2) if pending else ())
            for item in button["labels"]:
                self.cv.itemconfig(item, fill=fg)
            self.cv.itemconfig(button["marker"], text=self.tr('PROSES') if pending else (self.tr('✓ AKTIF') if active else ""), fill=fg)

    def set_big(self, which, ping, online, color):
        big, unit, tag = self.ids[f"{which}_big"], self.ids[f"{which}_unit"], self.ids[f"{which}_tag"]
        x0 = (26 if which == "wifi" else 258) if self.compact else (636 if which == "wifi" else 866)
        font_size, unit_y = (32, 108) if self.compact else (84, 204)
        if ping is not None:
            txt = f"{ping:.0f}"
            col = color
            tag_txt, tag_col = "● LIVE", (self.colors["blue"] if which == "wifi" else self.colors["lime_dk"])
            if ping >= 100:
                col, tag_txt, tag_col = self.colors["red"], "● HIGH", self.colors["red"]
        elif online:
            txt, col, tag_txt, tag_col = "RTO", self.colors["red"], "● RTO", self.colors["red"]
        else:
            txt, col, tag_txt, tag_col = "--", self.colors["line"], "○ OFFLINE", self.colors["dim"]
        self.cv.itemconfig(big, text=txt, fill=col)
        if self.compact:
            while font_size > 16 and self.measure(self.F(self.f_cond, font_size), txt) > 65:
                font_size -= 1
            self.cv.itemconfig(big, font=self.F(self.f_cond, font_size))
        w = self.measure(self.F(self.f_cond, font_size), txt)
        self.cv.coords(unit, *self.s(x0 + w + 6, unit_y))
        self.cv.itemconfig(unit, fill=col, state="normal" if ping is not None else "hidden")
        self.cv.itemconfig(tag, text=tag_txt, fill=tag_col)

    def draw_graph(self):
        gx1, gy1, gx2, gy2 = self.graph
        vals = [v for v in list(self.wifi_hist) + list(self.lan_hist) if v is not None]
        top = 100
        if vals:
            top = max(100, int((max(vals) * 1.2) // 50 + 1) * 50)
        for k, lbl in enumerate(self.grid_lbl):
            self.cv.itemconfig(lbl, text=str(int(top * (1 - k * 0.5))))
        py1, py2 = gy1 + 8, gy2 - 8
        n = 60
        for key, hist in (("wifi_line", self.wifi_hist), ("lan_line", self.lan_hist)):
            pts = []
            for i, v in enumerate(hist):
                x = gx1 + 4 + (gx2 - gx1 - 8) * i / (n - 1)
                if v is None:
                    y = py2
                else:
                    y = py2 - (py2 - py1) * min(v, top) / top
                pts += [x, y]
            self.cv.coords(self.ids[key], *self.s(*pts))
            has = any(v is not None for v in hist)
            self.cv.itemconfig(self.ids[key], state="normal" if has else "hidden")

    # --------------------------------------------------------------- actions
    def toggle(self, key):
        if self.busy.get(key):
            self.notify(self.tr('Perubahan masih diproses. Tunggu hingga selesai.'), "progress")
            return
        if key == "tun":
            self.tun_requested = not self.tun.is_running
            self.busy["tun"] = True

            def work():
                try:
                    if self.tun.is_running:
                        ok, msg = self.tun.stop()
                    else:
                        if not self.wifi:
                            self.scan_adapters()
                        if not self.wifi:
                            ok = False
                            msg = self.tr('Gagal: adapter Wi-Fi tidak ditemukan.')
                        else:
                            eth_alias = self.eth.get("alias") if self.eth_ip() else None
                            ok, msg = self.tun.start(self.wifi.get("alias", "Wi-Fi"), eth_alias)
                    self.notify(msg, "success" if ok else "error")
                finally:
                    self.busy["tun"] = False
            self.bg(work, self.tr('Mengubah status TUN Virtual Adapter...'))
        elif key == "proxy":
            self.busy["proxy"] = True

            def work():
                try:
                    if self.proxy and self.proxy.is_running:
                        self.stop_proxy()
                    else:
                        self.start_proxy()
                finally:
                    self.busy["proxy"] = False
            self.bg(work, self.tr('Mengubah status Proxy Engine...'))

    def start_proxy(self):
        self.proxy = DualNetProxy(get_outbound_ip_fn=self.eth_ip, http_port=8080, socks_port=1080)
        ready = threading.Event()

        def loop_main():
            self.proxy_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.proxy_loop)
            try:
                ok, msg = self.proxy_loop.run_until_complete(self.proxy.start())
                self.notify(msg, "success" if ok else "error")
                ready.set()
                if ok:
                    self.proxy_loop.run_forever()
            except Exception as e:
                self.notify(self.tr('Proxy gagal: {v0}', v0=e), "error")
                ready.set()
        threading.Thread(target=loop_main, daemon=True).start()
        ready.wait(5)

    def stop_proxy(self):
        if self.proxy and self.proxy_loop and self.proxy_loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self.proxy.stop(), self.proxy_loop).result(timeout=3)
            except Exception:
                pass
            self.proxy_loop.call_soon_threadsafe(self.proxy_loop.stop)
        if self.proxy:
            self.proxy.is_running = False
        self.notify(self.tr('Proxy engine dimatikan.'), "success")

    def apply_preset(self, key):
        if any(self.busy[k] for k in ("wifi", "lan", "preset")):
            self.notify(self.tr('Perubahan jaringan masih diproses. Tunggu hingga selesai.'), "progress")
            return
        if not self.wifi or not self.eth:
            self.notify(self.tr('Preset memerlukan adapter Wi-Fi dan LAN. Klik Refresh untuk mendeteksi keduanya.'), "error")
            return
        if detect_metric_preset(self.wifi, self.eth) == key:
            self.notify(self.tr('Preset {v0} sudah aktif.', v0='AUTO' if key == 'reset' else key.upper()), "success")
            return
        w_alias = (self.wifi or {}).get("alias", "Wi-Fi").replace("'", "''")
        e_alias = (self.eth or {}).get("alias", "Ethernet").replace("'", "''")
        if key == "gaming":
            ps = (f"Set-NetIPInterface -InterfaceAlias '{w_alias}' -AddressFamily IPv4 -AutomaticMetric Disabled -InterfaceMetric 10 -EA Stop;"
                  f"Set-NetIPInterface -InterfaceAlias '{e_alias}' -AddressFamily IPv4 -AutomaticMetric Disabled -InterfaceMetric 50 -EA Stop")
            done = self.tr('Mode Gaming diterapkan: Wi-Fi metric 10, LAN metric 50.')
        elif key == "download":
            ps = (f"Set-NetIPInterface -InterfaceAlias '{e_alias}' -AddressFamily IPv4 -AutomaticMetric Disabled -InterfaceMetric 10 -EA Stop;"
                  f"Set-NetIPInterface -InterfaceAlias '{w_alias}' -AddressFamily IPv4 -AutomaticMetric Disabled -InterfaceMetric 50 -EA Stop")
            done = self.tr('Mode Download diterapkan: LAN metric 10, Wi-Fi metric 50.')
        else:
            ps = (f"Set-NetIPInterface -InterfaceAlias '{w_alias}' -AddressFamily IPv4 -AutomaticMetric Enabled -EA Stop;"
                  f"Set-NetIPInterface -InterfaceAlias '{e_alias}' -AddressFamily IPv4 -AutomaticMetric Enabled -EA Stop")
            done = self.tr('Metric dikembalikan ke otomatis Windows.')

        def work():
            try:
                with self.adapter_lock:
                    proc = run_network_command(ps)
                    self.scan_adapters()
                    if proc.returncode != 0:
                        self.notify(self.tr('Preset gagal: {v0}', v0=proc.stderr.strip() or self.tr('izin Administrator dibatalkan / adapter tidak tersedia.')), "error")
                    elif detect_metric_preset(self.wifi, self.eth) == key:
                        self.notify(done, "success")
                    else:
                        self.notify(self.tr('Perintah preset selesai, tetapi pengaturan belum terkonfirmasi. Status menampilkan metric Windows yang terbaca.'), "error")
                self.wake.set()
            finally:
                self.busy["preset"] = False
                self.pending_preset = None
        self.busy["preset"] = True
        self.pending_preset = key
        self.bg(work, self.tr('Menerapkan preset metric...'))

    def flash_nav(self, key):
        box, chk = self.nav_box[key]
        self.cv.itemconfig(chk, state="normal")
        self.root.after(700, lambda: self.cv.itemconfig(chk, state="hidden"))

    def nav_action(self, key):
        if key == "autostart":
            if self.busy["autostart"]:
                self.notify(self.tr('Pengaturan autostart masih diproses...'), "progress")
                return
            def work():
                try:
                    if self.autostart_on:
                        ok, msg = uninstall_autostart()
                    else:
                        ok, msg = install_autostart()
                    self.autostart_on = is_autostart_installed()
                    self.notify(msg, "success" if ok else "error")
                finally:
                    self.busy["autostart"] = False
            self.busy["autostart"] = True
            self.bg(work, self.tr('Mengubah autostart Windows...'))
            return
        self.flash_nav(key)
        if key == "rules":
            try:
                os.startfile(RULES_PATH)
            except OSError:
                subprocess.Popen(["notepad.exe", RULES_PATH], creationflags=0)
            self.notify(self.tr('Rules dibuka. Simpan perubahan lalu matikan dan aktifkan lagi TUN untuk menerapkannya.'), "success")
        elif key == "steam":
            steam = find_steam()
            if steam:
                os.startfile(steam, arguments="-proxy-server=http://127.0.0.1:8080")
                self.notify(self.tr('Steam dibuka via LAN proxy (127.0.0.1:8080). Pastikan Proxy aktif.'), "success")
            else:
                self.notify(self.tr('Steam tidak ditemukan. Instal Steam atau tambahkan ke PATH.'), "error")
        elif key == "guide":
            self.show_guide()
        elif key == "refresh":
            self.adapter_scan_requested = True
            self.wake.set()
            self.notify(self.tr('Memindai ulang adapter, alamat IP, dan ping...'), "progress")
        elif key == "tray":
            self.to_tray()

    def show_guide(self):
        if self.compact:
            self.set_display_preference("compact", False)
        self.tour_step = 0
        self.draw_tour()
        self.notify(self.tr('Tutorial dimulai. Gunakan Lanjut / Kembali; Esc untuk menutup.'))

    def maybe_show_guide(self):
        if self.compact:
            return
        try:
            with open(UI_STATE_PATH, encoding="utf-8") as f:
                if json.load(f).get("tutorial_seen"):
                    return
        except (OSError, ValueError, AttributeError):
            pass
        if self.tour_step is None:
            self.show_guide()

    def draw_tour(self):
        steps = [
            (self.tr('Selamat datang di DualNet'), self.tr('Pisahkan jalur gaming dan download dalam beberapa langkah.\n\nTutorial ini akan menyorot kontrol utama. Baca panduannya, lalu coba setelah tutorial selesai.\n\nBisa dibuka lagi lewat Guide atau F1.'), None, (350, 190)),
            (self.tr('1. Periksa kedua jaringan'), self.tr('Sambungkan Wi-Fi dan kabel LAN. ONLINE berarti koneksi siap.\n\nGunakan MATIKAN / AKTIFKAN atau kotak centang untuk mengubah status adapter. NO LINK berarti adapter aktif tapi belum tersambung; NONAKTIF berarti dimatikan.\n\nJika IP belum muncul, klik Refresh.'), (166, 108, 604, 310), (640, 240)),
            (self.tr('2. Pantau ping dan bandwidth'), self.tr('Ping lebih kecil berarti respons lebih cepat.\n\n↓ adalah kecepatan download, ↑ kecepatan upload per adapter. KB/s dan MB/s menyesuaikan otomatis; angka mencakup seluruh traffic adapter.\n\nRTO berarti ping tidak mendapat balasan. Grafik menampilkan riwayat ping.'), (632, 112, 1088, 426), (182, 205)),
            (self.tr('3. Aktifkan TUN Mode'), self.tr('Klik kartu TUN Mode untuk memisahkan traffic otomatis. Tunggu status ACTIVE.\n\nAplikasi download di Rules diarahkan ke LAN; game dan Discord ke Wi-Fi. Jika LAN belum siap saat diaktifkan, jalur memakai Wi-Fi.\n\nTUN memerlukan izin Administrator.'), (166, 324, 604, 418), (640, 225)),
            (self.tr('4. Pilih prioritas jaringan'), self.tr('GAMING: Wi-Fi prioritas utama Windows.\nDOWNLOAD: LAN prioritas utama.\nAUTO: metric otomatis Windows.\n\nPreset yang aktif berwarna dan bertanda ✓ AKTIF. Status diverifikasi dari Windows; CUSTOM berarti pengaturan berbeda dari preset.\n\nPemisahan per aplikasi tetap dilakukan oleh TUN.'), (634, 434, 1086, 518), (182, 200)),
            (self.tr('5. Proxy jika diperlukan'), self.tr('Mode Proxy digunakan untuk aplikasi yang disetel manual. Klik kartu lalu tunggu ACTIVE.\n\nHTTP: 127.0.0.1:8080\nSOCKS5: 127.0.0.1:1080\n\nUntuk pemakaian biasa dengan TUN, Proxy boleh tetap OFF.'), (166, 432, 604, 526), (640, 220)),
            (self.tr('6. Atur aplikasi dan autostart'), self.tr('Edit Rules membuka daftar aplikasi untuk jalur LAN / Wi-Fi. Simpan, lalu matikan dan aktifkan lagi TUN.\n\nAutostart menjalankan DualNet di tray dan mengaktifkan TUN saat login Windows. Centang berubah setelah pengaturan berhasil.\n\nRefresh memindai ulang adapter.'), (166, 616, 734, 644), (640, 285)),
            (self.tr('7. Tray dan feedback'), self.tr('Tray atau kotak biru menyembunyikan jendela; layanan tetap berjalan. Buka lagi lewat ikon DualNet dekat jam Windows.\n\n× di dalam aplikasi berarti keluar dan menghentikan layanan.\n\nPanel bawah menampilkan proses, hasil berhasil, atau pesan gagal dari tombol yang kamu klik.'), (742, 614, 1082, 646), (640, 285)),
        ]
        self.tour_steps = len(steps)
        title, body, focus, position = steps[self.tour_step]
        cv = self.cv
        cv.delete("tour")
        cv.delete("tour_button")
        # Block clicks even in the highlighted area; the tour never runs actions.
        if focus:
            x1, y1, x2, y2 = focus
            for bounds in ((0, 0, W, y1), (0, y2, W, H), (0, y1, x1, y2), (x2, y1, W, y2)):
                self.rect(*bounds, fill=self.colors["ink"], stipple="gray50", tags=("tour",))
            self.rect(*focus, fill=self.colors["blue_lt"], stipple="gray12", outline=self.colors["blue"], width=3, tags=("tour",))
        else:
            self.rect(0, 0, W, H, fill=self.colors["ink"], stipple="gray50", tags=("tour",))
        cv.tag_bind("tour", "<Button-1>", lambda e: "break")
        x, y = position
        self.rect(x + 5, y + 5, x + 425, y + 335, fill=self.colors["ink"], tags=("tour",))
        self.rect(x, y, x + 420, y + 330, fill=self.colors["panel"], outline=self.colors["blue"], width=2, tags=("tour",))
        self.rect(x, y, x + 420, y + 5, fill=self.colors["blue"], tags=("tour",))
        self.text(x + 22, y + 26, self.tr('PANDUAN  {v0:02d} / {v1:02d}', v0=self.tour_step + 1, v1=len(steps)),
                  self.F(self.f_mono, 11), fill=self.colors["blue"], tags=("tour",))
        self.text(x + 22, y + 57, title, self.F(self.f_disp, 22), tags=("tour",))
        self.text(x + 22, y + 82, body, self.F(self.f_disp, 14), anchor="nw", width=376 * self.S,
                  tags=("tour",))
        self.line(x + 22, y + 272, x + 398, y + 272, fill=self.colors["line"], tags=("tour",))
        if self.tour_step:
            self._tour_button(x + 22, y + 286, 94, self.tr('← Kembali'), lambda: self.advance_tour(-1))
        self._tour_button(x + 133, y + 286, 102, self.tr('Lewati'), self.end_tour)
        label = self.tr('Selesai ✓') if self.tour_step == len(steps) - 1 else self.tr('Lanjut →')
        self._tour_button(x + 278, y + 282, 120, label, lambda: self.advance_tour(1), primary=True)
        cv.tag_raise("tour")
        cv.tag_raise("tour_button")

    def _tour_button(self, x, y, width, label, callback, primary=False):
        tag = f"tour_button_{label}"
        self.rect(x, y, x + width, y + 32, fill=self.colors["blue"] if primary else self.colors["panel"],
                  outline=self.colors["blue"] if primary else self.colors["line"], tags=("tour", tag))
        self.text(x + width / 2, y + 16, label, self.F(self.f_mono, 12), anchor="center",
                  fill=self.colors["on_accent"] if primary else self.colors["ink"], tags=("tour", tag))
        self.clickable(tag, callback)
        # Canvas evaluates tag bindings in order. Buttons handle their own clicks.
        self.cv.dtag(tag, "tour")
        self.cv.addtag_withtag("tour_button", tag)

    def advance_tour(self, delta):
        self.cv.delete("tour_button")
        self.tour_step += delta
        if self.tour_step >= self.tour_steps:
            self.end_tour(completed=True)
        else:
            self.draw_tour()

    def end_tour(self, completed=False):
        self.cv.delete("tour")
        self.cv.delete("tour_button")
        self.cv.config(cursor="")
        self.tour_step = None
        save_settings(UI_STATE_PATH, tutorial_seen=True)
        self.notify(self.tr('Tutorial selesai. Sambungkan kedua jaringan, lalu aktifkan TUN Mode.') if completed
                    else self.tr('Tutorial ditutup. Buka lagi kapan saja lewat Guide atau F1.'), "success" if completed else "info")

    # ----------------------------------------------------------- tray / exit
    def _tray_image(self):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rectangle([4, 4, 60, 60], fill=self.colors["blue"])
        d.rectangle([16, 16, 48, 48], outline="white", width=5)
        d.line([22, 32, 30, 40, 44, 24], fill="white", width=5)
        return img

    def start_tray(self):
        if self.tray_starting or self.tray_ready.is_set():
            return
        if not HAS_TRAY:
            self.tray_error = self.tr('Library tray belum tersedia. Pasang pystray dan Pillow, lalu jalankan ulang.')
            return
        self.tray_starting = True
        self.tray_started_at = time.monotonic()
        self.tray_warned = False
        self.tray_error = ""
        def run():
            icon = None
            try:
                menu = pystray.Menu(
                    pystray.MenuItem(lambda item: self.tr('Buka DualNet'), lambda *a: self.tray_actions.put("show"), default=True),
                    pystray.MenuItem(lambda item: self.tr('Toggle TUN Mode'), lambda *a: self.tray_actions.put("tun")),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem(lambda item: self.tr('Keluar'), lambda *a: self.tray_actions.put("quit")),
                )
                icon = pystray.Icon("DualNet", self._tray_image(), "DualNet Splitter", menu)
                self.tray_icon = icon
                def setup(ready_icon):
                    try:
                        if self.running:
                            ready_icon.visible = True
                            self.tray_ready.set()
                        else:
                            ready_icon.stop()
                    except Exception as exc:
                        self.tray_error = self.tr('Ikon tray gagal ditampilkan: {v0}', v0=exc)
                        ready_icon.stop()
                icon.run(setup=setup)
            except Exception as exc:
                self.tray_error = self.tr('Ikon tray gagal dibuat: {v0}', v0=exc)
            finally:
                self.tray_ready.clear()
                self.tray_starting = False
                self.tray_icon = None
                if self.running and not self.tray_error:
                    self.tray_error = self.tr('Ikon tray berhenti. Jendela DualNet dibuka kembali.')
        threading.Thread(target=run, daemon=True).start()

    def poll_tray(self):
        if not self.running:
            return
        while not self.tray_actions.empty():
            action = self.tray_actions.get()
            if action == "show":
                self.show_window()
            elif action == "tun":
                self.toggle("tun")
            elif action == "quit":
                self.quit_app()
                return
        if self.tray_ready.is_set() and self.tray_hide_pending:
            self.tray_hide_pending = False
            self.root.withdraw()
            self.notify(self.tr('DualNet berjalan di tray. Klik ikon DualNet dekat jam untuk membuka kembali.'), "success")
        if (self.tray_starting and not self.tray_ready.is_set() and not self.tray_warned
                and time.monotonic() - self.tray_started_at > 10):
            self.tray_warned = True
            self.tray_error = self.tr('Ikon tray belum siap. Jendela tetap dibuka agar aplikasi bisa diakses.')
        if self.tray_error:
            error = self.tray_error
            self.tray_error = ""
            self.show_window()
            self.notify(error, "error")
        self.root.after(100, self.poll_tray)

    def to_tray(self):
        if self.tour_step is not None:
            self.end_tour()
        if self.tray_ready.is_set():
            self.root.withdraw()
            self.notify(self.tr('DualNet berjalan di tray. Klik ikon DualNet dekat jam untuk membuka kembali.'), "success")
        else:
            self.tray_hide_pending = True
            self.notify(self.tr('Menyiapkan ikon tray sebelum menyembunyikan jendela...'), "progress")
            self.start_tray()

    def show_window(self):
        self.tray_hide_pending = False
        self.root.deiconify()
        self.apply_window_priority()
        self.root.lift()
        self.root.focus_force()

    def on_close(self):
        # Keep routing alive in the tray if TUN is active, otherwise exit.
        if self.tun.is_running and HAS_TRAY:
            self.to_tray()
        else:
            self.quit_app()

    def quit_app(self):
        self.running = False
        self.wake.set()
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        try:
            if self.tun.is_running:
                self.tun.stop()
        except Exception:
            pass
        try:
            self.stop_proxy()
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def run_application(autostart=False):
    global _show_event
    if not single_instance("Global\\DualNetSplitterGUI"):
        if not autostart:
            k32 = ctypes.windll.kernel32
            k32.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p]
            k32.OpenEventW.restype = ctypes.c_void_p
            handle = k32.OpenEventW(0x0002, False, SHOW_EVENT_NAME)
            if handle:
                k32.SetEvent(ctypes.c_void_p(handle))
                k32.CloseHandle(ctypes.c_void_p(handle))
            else:
                ctypes.windll.user32.MessageBoxW(None, "DualNet versi sebelumnya masih berjalan. Tutup lalu buka kembali untuk memakai pembaruan.",
                                                 "DualNet Splitter", 0x40)
        return
    k32 = ctypes.windll.kernel32
    k32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_wchar_p]
    k32.CreateEventW.restype = ctypes.c_void_p
    _show_event = k32.CreateEventW(None, False, False, SHOW_EVENT_NAME)
    try:
        DualNetApp(autostart=autostart).run()
    finally:
        if _show_event:
            k32.CloseHandle(ctypes.c_void_p(_show_event))
            _show_event = None


if __name__ == "__main__":
    run_application(autostart="--autostart" in sys.argv)
