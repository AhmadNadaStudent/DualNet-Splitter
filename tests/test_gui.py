import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import gui


class TraySafetyTests(unittest.TestCase):
    def make_app(self):
        app = gui.DualNetApp.__new__(gui.DualNetApp)
        app.root = Mock()
        app.tour_step = None
        app.tray_ready = threading.Event()
        app.tray_starting = False
        app.start_tray = Mock()
        return app

    def test_hide_waits_for_registered_icon(self):
        app = self.make_app()
        app.to_tray()
        app.root.withdraw.assert_not_called()
        self.assertTrue(app.tray_hide_pending)
        self.assertEqual(app.feedback_kind, "progress")

    def test_ready_tray_can_hide(self):
        app = self.make_app()
        app.tray_ready.set()
        app.to_tray()
        app.root.withdraw.assert_called_once()

    def test_failed_tray_restores_window(self):
        import queue
        app = self.make_app()
        app.running = True
        app.tray_actions = queue.SimpleQueue()
        app.tray_hide_pending = True
        app.tray_error = "Ikon gagal"
        app.poll_tray()
        app.root.deiconify.assert_called_once()
        self.assertEqual(app.feedback_kind, "error")
        self.assertFalse(app.tray_hide_pending)


class CanvasTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [patch("gui.threading.Thread.start"), patch.object(gui.DualNetApp, "start_tray"),
                        patch("gui.TUNManager"), patch("gui.UI_STATE_PATH", str(Path(self.temp.name) / "ui.json"))]
        for p in self.patches:
            p.start()
        self.app = gui.DualNetApp()
        self.app.root.withdraw()
        self.app.tun.is_running = False

    def tearDown(self):
        self.app.running = False
        self.app.root.destroy()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_tour_all_steps_body_fits_and_buttons_are_on_top(self):
        app = self.app
        app.show_guide()
        for i in range(app.tour_steps):
            tour = app.cv.find_withtag("tour")
            buttons = app.cv.find_withtag("tour_button")
            self.assertTrue(buttons)
            stacking = app.cv.find_all()
            self.assertGreater(min(stacking.index(v) for v in buttons), max(stacking.index(v) for v in tour))
            texts = [v for v in tour if app.cv.type(v) == "text"]
            body_bbox = app.cv.bbox(texts[-1])
            divider = [v for v in tour if app.cv.type(v) == "line"][-1]
            self.assertLess(body_bbox[3], app.cv.coords(divider)[1], f"Body overlaps controls at step {i}")
            app.advance_tour(1)
        self.assertIsNone(app.tour_step)
        self.assertEqual(app.cv.find_withtag("tour"), ())
        self.assertEqual(app.cv.find_withtag("tour_button"), ())
        self.assertTrue(Path(gui.UI_STATE_PATH).exists())

    def test_bandwidth_fits_below_each_ping(self):
        app = self.app
        for key, right in (("wifi", 854), ("lan", 1082)):
            item = app.ids[f"{key}_bandwidth"]
            app.cv.itemconfig(item, text="↓ 999.9 MB/s  ↑ 999.9 MB/s")
            x1, y1, x2, y2 = app.cv.bbox(item)
            self.assertLessEqual(x2, right * app.S)
            self.assertGreaterEqual(y1, 214 * app.S)
            self.assertLess(y2, 246 * app.S)

    def test_preset_failure_is_reported(self):
        app = self.app
        app.bg = lambda fn, msg=None: fn()
        app.wifi = {"alias": "Wi-Fi", "metric": 50, "automatic_metric": False}
        app.eth = {"alias": "Ethernet", "metric": 10, "automatic_metric": False}
        with patch.object(app, "scan_adapters"), patch("gui.run_network_command", return_value=Mock(returncode=1, stderr="adapter gagal")):
            app.apply_preset("gaming")
        self.assertEqual(app.feedback_kind, "error")
        self.assertIn("adapter gagal", app.status)
        self.assertFalse(app.busy["preset"])

    def test_active_preset_changes_only_after_verification(self):
        app = self.app
        app.wifi = {"alias": "Wi-Fi", "metric": 50, "automatic_metric": False}
        app.eth = {"alias": "Ethernet", "metric": 10, "automatic_metric": False}
        pending = []
        app.bg = lambda fn, msg=None: pending.append(fn)
        app.apply_preset("gaming")
        app.update_preset_ui()
        self.assertEqual(app.cv.itemcget(app.ids["active_preset"], "text"), "MENERAPKAN: GAMING")
        self.assertEqual(app.cv.itemcget(app.preset_buttons["download"]["marker"], "text"), "✓ AKTIF")
        def scan():
            app.wifi["metric"], app.eth["metric"] = 10, 50
        with patch.object(app, "scan_adapters", side_effect=scan), patch("gui.run_network_command", return_value=Mock(returncode=0)):
            pending[0]()
        app.update_preset_ui()
        self.assertEqual(app.cv.itemcget(app.ids["active_preset"], "text"), "AKTIF: GAMING")
        self.assertEqual(app.cv.itemcget(app.preset_buttons["gaming"]["marker"], "text"), "✓ AKTIF")
        self.assertEqual(app.cv.itemcget(app.preset_buttons["download"]["marker"], "text"), "")

    def test_preset_mismatch_is_not_shown_as_success(self):
        app = self.app
        app.bg = lambda fn, msg=None: fn()
        app.wifi = {"alias": "Wi-Fi", "metric": 50, "automatic_metric": False}
        app.eth = {"alias": "Ethernet", "metric": 10, "automatic_metric": False}
        with patch.object(app, "scan_adapters"), patch("gui.run_network_command", return_value=Mock(returncode=0)):
            app.apply_preset("gaming")
        self.assertEqual(app.feedback_kind, "error")
        app.update_preset_ui()
        self.assertEqual(app.cv.itemcget(app.ids["active_preset"], "text"), "AKTIF: DOWNLOAD")

    def test_disconnected_adapter_can_be_disabled_and_reenabled(self):
        app = self.app
        app.bg = lambda fn, msg=None: fn()
        app.eth = {"alias": "Ethernet", "index": 5, "enabled": True, "state": "Disconnected"}
        def disable():
            app.eth.update(enabled=False, state="Disabled")
        with patch("gui.set_adapter_enabled", return_value=Mock(returncode=0)) as setter, patch.object(app, "scan_adapters", side_effect=disable):
            app.toggle_adapter("lan")
            setter.assert_called_once_with(5, False)
        app.update_adapter_card("lan", app.eth, None)
        self.assertEqual(app.cv.itemcget(app.cards["lan"]["state"], "text"), "NONAKTIF")
        self.assertEqual(app.cv.itemcget(app.cards["lan"]["adapter_action"], "text"), "⏻ AKTIFKAN")
        def enable():
            app.eth.update(enabled=True, state="Disconnected")
        with patch("gui.set_adapter_enabled", return_value=Mock(returncode=0)) as setter, patch.object(app, "scan_adapters", side_effect=enable):
            app.toggle_adapter("lan")
            setter.assert_called_once_with(5, True)
        app.update_adapter_card("lan", app.eth, None)
        self.assertEqual(app.cv.itemcget(app.cards["lan"]["state"], "text"), "NO LINK")
        self.assertEqual(app.cv.itemcget(app.cards["lan"]["adapter_action"], "text"), "⏻ MATIKAN")

    def test_adapter_failure_preserves_observed_state(self):
        app = self.app
        app.bg = lambda fn, msg=None: fn()
        app.wifi = {"index": 10, "enabled": True, "state": "Up"}
        with patch("gui.set_adapter_enabled", return_value=Mock(returncode=1, stderr="Access denied")):
            app.toggle_adapter("wifi")
        self.assertTrue(app.wifi["enabled"])
        self.assertFalse(app.busy["wifi"])
        self.assertEqual(app.feedback_kind, "error")

    def test_power_controls_fit_between_title_and_number(self):
        app = self.app
        for key in ("wifi", "lan"):
            app.update_adapter_card(key, {"enabled": False, "state": "Disabled"}, None)
            d = app.cards[key]
            title = app.cv.bbox(d["title"])
            button = app.cv.bbox(d["adapter_button"])
            action = app.cv.bbox(d["adapter_action"])
            number = app.cv.bbox(d["num"])
            self.assertLess(title[2], button[0])
            self.assertLess(button[2], number[0])
            self.assertGreaterEqual(action[0], button[0])
            self.assertLessEqual(action[2], button[2])

    def test_tun_failure_is_reported(self):
        app = self.app
        app.bg = lambda fn, msg=None: fn()
        app.wifi = {"alias": "Wi-Fi", "state": "Up", "ip": "192.0.2.1"}
        app.tun.start.return_value = (False, "Gagal mengaktifkan TUN")
        app.toggle("tun")
        self.assertEqual(app.feedback_kind, "error")
        self.assertFalse(app.busy["tun"])

    def test_display_changes_preserve_services_history_and_one_refresh_loop(self):
        app = self.app
        app.wifi = {"enabled": True, "state": "Up", "ip": "192.0.2.1"}
        app.wifi_ping = 24
        app.wifi_hist.append(24)
        app.bandwidth["wifi"] = (12_000_000, 250_000)
        tun, sampler, history = app.tun, app.bandwidth_sampler, app.wifi_hist
        app.pending_preset = "gaming"
        with patch.object(app.root, "after", wraps=app.root.after) as after:
            for key, value in (("theme", "dark"), ("language", "en"), ("compact", True),
                               ("theme", "light"), ("compact", False)):
                app.set_display_preference(key, value)
            after.assert_not_called()
        self.assertIs(app.tun, tun)
        self.assertIs(app.bandwidth_sampler, sampler)
        self.assertIs(app.wifi_hist, history)
        self.assertEqual(app.pending_preset, "gaming")
        self.assertIn("12.0 MB/s", app.cv.itemcget(app.ids["wifi_bandwidth"], "text"))
        app.tun.start.assert_not_called()
        app.tun.stop.assert_not_called()

    def test_compact_is_monitoring_only_and_telemetry_fits_both_languages(self):
        app = self.app
        for language in ("id", "en"):
            app.set_display_preference("language", language)
            app.set_display_preference("compact", True)
            self.assertEqual(app.cards, {})
            for key in ("tun", "proxy", "wifi", "lan"):
                self.assertEqual(app.cv.find_withtag(f"card_{key}"), ())
            self.assertTrue(app.cv.find_withtag("display_compact"))
            for key, right in (("wifi", 232), ("lan", 464)):
                item = app.ids[f"{key}_bandwidth"]
                app.cv.itemconfig(item, text="↓ 999.9 GB/s  ↑ 999.9 GB/s")
                self.assertLessEqual(app.cv.bbox(item)[2], right * app.S)
            app.notify("Very long feedback with {braces} " * 20, "error")
            app.refresh_ui(schedule=False)
            self.assertLessEqual(app.cv.bbox(app.ids["status"])[2], 466 * app.S)

    def test_english_adapter_preset_and_live_feedback(self):
        app = self.app
        app.notify(app.tr("Adapter {v0} belum ditemukan. Klik Refresh.", v0="Wi-Fi"), "error")
        app.set_display_preference("language", "en")
        self.assertEqual(app.cv.itemcget(app.ids["status"], "text"), "Adapter Wi-Fi was not found. Click Refresh.")
        app.update_adapter_card("lan", {"enabled": False, "state": "Disabled"}, None)
        self.assertEqual(app.cv.itemcget(app.cards["lan"]["state"], "text"), "DISABLED")
        self.assertEqual(app.cv.itemcget(app.cards["lan"]["adapter_action"], "text"), "⏻ ENABLE")
        app.pending_preset = "gaming"
        app.update_preset_ui()
        self.assertEqual(app.cv.itemcget(app.ids["active_preset"], "text"), "APPLYING: GAMING")
        app.set_display_preference("language", "id")
        self.assertEqual(app.cv.itemcget(app.ids["status"], "text"), "Adapter Wi-Fi belum ditemukan. Klik Refresh.")

    def test_english_tour_fits_and_guide_expands_compact(self):
        app = self.app
        app.set_display_preference("theme", "dark")
        app.set_display_preference("language", "en")
        app.set_display_preference("compact", True)
        app.show_guide()
        self.assertFalse(app.compact)
        for i in range(app.tour_steps):
            items = app.cv.find_withtag("tour")
            texts = [v for v in items if app.cv.type(v) == "text"]
            body = app.cv.bbox(texts[-1])
            divider = [v for v in items if app.cv.type(v) == "line"][-1]
            self.assertLess(body[3], app.cv.coords(divider)[1], f"English body overlaps controls at step {i}")
            app.advance_tour(1)

    def test_preferences_are_restored_on_next_launch(self):
        app = self.app
        for key, value in (("theme", "dark"), ("language", "en"), ("compact", True), ("compact_topmost", True)):
            app.set_display_preference(key, value)
        other = gui.DualNetApp()
        try:
            other.root.withdraw()
            self.assertTrue(other.compact)
            self.assertEqual(other.theme, "dark")
            self.assertEqual(other.language, "en")
            self.assertEqual(other.cv.cget("bg"), gui.PALETTES["dark"]["bg"])
            self.assertTrue(other.cv.find_withtag("display_compact"))
            self.assertEqual(other.cards, {})
            self.assertTrue(other.compact_topmost)
        finally:
            other.running = False
            other.root.destroy()

    def test_always_on_top_applies_only_in_compact_and_survives_display_changes(self):
        app = self.app
        with patch.object(app.root, "attributes", wraps=app.root.attributes) as attributes:
            app.set_display_preference("compact_topmost", True)
            attributes.assert_called_with("-topmost", False)
            app.set_display_preference("compact", True)
            attributes.assert_called_with("-topmost", True)
            for key, value in (("theme", "dark"), ("language", "en")):
                app.set_display_preference(key, value)
                attributes.assert_called_with("-topmost", True)
            pin = [v for v in app.cv.find_withtag("display_compact_topmost") if app.cv.type(v) == "text"][0]
            self.assertEqual(app.cv.itemcget(pin, "text"), "✓ Always on Top")
            app.set_display_preference("compact", False)
            attributes.assert_called_with("-topmost", False)
            self.assertTrue(app.compact_topmost)
            app.set_display_preference("compact", True)
            attributes.assert_called_with("-topmost", True)
            app.set_display_preference("compact_topmost", False)
            attributes.assert_called_with("-topmost", False)

    def test_half_height_compact_keeps_content_inside_window(self):
        app = self.app
        self.assertEqual(gui.COMPACT_H, 180)
        for language in ("id", "en"):
            for topmost in (False, True):
                app.set_display_preference("language", language)
                app.set_display_preference("compact_topmost", topmost)
                app.set_display_preference("compact", True)
                app.wifi = app.eth = {"enabled": True, "state": "Up", "ip": "255.255.255.255"}
                app.wifi_ping = app.eth_ping = 9999
                app.bandwidth = {"wifi": (999_900_000_000, 999_900_000_000),
                                 "lan": (999_900_000_000, 999_900_000_000)}
                app.refresh_ui(schedule=False)
                for key in ("wifi", "lan"):
                    self.assertLess(app.cv.bbox(app.ids[f"{key}_unit"])[2],
                                    app.cv.bbox(app.ids[f"{key}_ip"])[0])
                for item in app.cv.find_all():
                    bounds = app.cv.bbox(item)
                    if bounds:
                        self.assertGreaterEqual(bounds[1], -1)
                        self.assertLessEqual(bounds[3], gui.COMPACT_H * app.S + 1)
                        self.assertLessEqual(bounds[2], gui.COMPACT_W * app.S + 1)


if __name__ == "__main__":
    unittest.main()
