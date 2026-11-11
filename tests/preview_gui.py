"""Privacy-safe demo: synthetic telemetry, inert controls, no network access.
Run: python tests/preview_gui.py [--dark] [--compact]
"""
import argparse
import math
import sys
import tempfile
from collections import deque
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gui
from engine.tun_manager import DEFAULT_RULES

class PreviewTUN:
    is_running = True
    rules = DEFAULT_RULES
    def stop(self):
        self.is_running = False
        return True, "Preview only"

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dark", action="store_true")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="dualnet-preview-") as directory:
        settings = str(Path(directory) / "ui.json")
        gui.save_settings(settings, tutorial_seen=True, language="en", theme="dark" if args.dark else "light", compact=args.compact)
        with (patch("gui.UI_STATE_PATH", settings), patch("gui.TUNManager", PreviewTUN),
              patch.object(gui.DualNetApp, "monitor_worker"), patch.object(gui.DualNetApp, "bandwidth_worker"),
              patch.object(gui.DualNetApp, "_load_autostart_state"), patch.object(gui.DualNetApp, "start_tray")):
            app = gui.DualNetApp()
            app.root.title("DualNet Splitter - Demo Preview")
            base = {"enabled": True, "state": "Up", "automatic_metric": False}
            # RFC 5737 documentation addresses; never read from this computer.
            app.wifi = dict(base, alias="Wireless demo", ip="192.0.2.10", index=10, metric=10)
            app.eth = dict(base, alias="Ethernet demo", ip="198.51.100.20", index=20, metric=50)
            app.wifi_ping, app.eth_ping = 18.4, 32.6
            app.bandwidth = {"wifi": (240_000, 65_000), "lan": (42_500_000, 1_200_000)}
            app.wifi_hist = deque((18 + math.sin(i * .5) * 3 for i in range(60)), maxlen=60)
            app.lan_hist = deque((34 + math.sin(i * .35) * 7 for i in range(60)), maxlen=60)
            app.notify("DEMO PREVIEW - synthetic data only; network controls are disabled.")
            for name in ("apply_preset", "toggle_adapter", "toggle", "nav_action", "to_tray"):
                setattr(app, name, lambda *a: app.notify("Demo preview: network controls are disabled."))
            app.root.after(900_000, app.quit_app)
            app.run()

if __name__ == "__main__":
    main()
