import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from engine.network import find_primary_adapters, generate_metric_ps_script
from engine.tun_manager import _install_archive, binary_architecture
from engine.autostart import install_autostart


class PortabilityTests(unittest.TestCase):
    def test_renamed_adapters_and_virtual_exclusion(self):
        rows = {
            "Office radio": {"interface_type": 71, "hardware": True, "state": "Up", "index": 4},
            "USB dock": {"interface_type": 6, "hardware": True, "state": "Up", "index": 8},
            "Ethernet": {"interface_type": 6, "hardware": True, "state": "Disconnected", "index": 2},
            "Tunnel": {"interface_type": 6, "hardware": False, "state": "Up", "index": 1},
        }
        with patch("engine.network.get_network_interfaces", return_value=rows):
            wifi, ethernet = find_primary_adapters()
        self.assertIs(wifi, rows["Office radio"])
        self.assertIs(ethernet, rows["USB dock"])

    def test_metric_script_targets_detected_indexes(self):
        with patch("engine.network.find_primary_adapters", return_value=({"index": 4}, {"index": 8})):
            self.assertIn("-InterfaceIndex 4", generate_metric_ps_script())
            self.assertIn("-InterfaceIndex 8", generate_metric_ps_script())
            self.assertNotIn("-InterfaceMetric", generate_metric_ps_script(automatic=True))
        with patch("engine.network.find_primary_adapters", return_value=(None, None)):
            with self.assertRaises(ValueError):
                generate_metric_ps_script()

    def test_architecture_matches_process(self):
        with patch("engine.tun_manager.struct.calcsize", return_value=8):
            with patch.dict("os.environ", {"PROCESSOR_ARCHITECTURE": "ARM64"}):
                self.assertEqual(binary_architecture(), "arm64")
            with patch.dict("os.environ", {"PROCESSOR_ARCHITECTURE": "AMD64"}):
                self.assertEqual(binary_architecture(), "amd64")
        with patch("engine.tun_manager.struct.calcsize", return_value=4):
            self.assertEqual(binary_architecture(), "386")

    def test_checksum_rejects_tampering_before_install(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("runtime/sing-box.exe", b"demo runtime")
        content = stream.getvalue()
        with tempfile.TemporaryDirectory() as directory:
            target = str(Path(directory) / "runtime.exe")
            response = Mock()
            response.__enter__ = Mock(return_value=Mock(read=lambda: content))
            response.__exit__ = Mock(return_value=False)
            with patch("engine.tun_manager.urllib.request.urlopen", return_value=response):
                with self.assertRaises(ValueError):
                    _install_archive("https://example.com/runtime.zip", "0" * 64, "/sing-box.exe", target)
                self.assertFalse(Path(target).exists())
                _install_archive("https://example.com/runtime.zip", hashlib.sha256(content).hexdigest(), "/sing-box.exe", target)
                self.assertEqual(Path(target).read_bytes(), b"demo runtime")

    def test_autostart_escapes_apostrophes(self):
        with (patch("engine.autostart.BASE_DIR", "C:/Demo's app"),
              patch("engine.autostart.IS_FROZEN", True),
              patch("engine.autostart.sys.executable", "C:/Demo's app/DualNet.exe"),
              patch("engine.autostart.subprocess.run", return_value=Mock(returncode=0)) as run):
            self.assertTrue(install_autostart()[0])
            self.assertIn("C:/Demo''s app", run.call_args.args[0][-1])

    def test_tun_stop_does_not_kill_other_apps(self):
        from engine.tun_manager import TUNManager
        manager = TUNManager()
        with patch("engine.tun_manager.subprocess.run") as run:
            manager.stop()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
