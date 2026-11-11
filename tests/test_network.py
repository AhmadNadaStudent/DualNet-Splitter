import json
import unittest
from unittest.mock import Mock, patch

from engine.network import (adapter_enabled, detect_metric_preset, get_network_interfaces,
                            run_network_command, set_adapter_enabled)


class NetworkControlTests(unittest.TestCase):
    def test_adapter_admin_state_is_separate_from_link_state(self):
        self.assertTrue(adapter_enabled({"state": "Disconnected", "enabled": True}))
        self.assertFalse(adapter_enabled({"state": "Disabled", "enabled": False}))
        self.assertFalse(adapter_enabled(None))

    def test_presets_require_matching_automatic_and_metric_settings(self):
        wifi = {"metric": 10, "automatic_metric": False}
        lan = {"metric": 50, "automatic_metric": False}
        self.assertEqual(detect_metric_preset(wifi, lan), "gaming")
        self.assertEqual(detect_metric_preset(lan, wifi), "download")
        self.assertEqual(detect_metric_preset(dict(wifi, automatic_metric=True), dict(lan, automatic_metric=True)), "reset")
        self.assertEqual(detect_metric_preset(dict(wifi, automatic_metric=True), lan), "custom")
        self.assertEqual(detect_metric_preset(dict(wifi, metric=20), lan), "custom")
        self.assertEqual(detect_metric_preset(wifi, None), "unknown")
        self.assertEqual(detect_metric_preset({"metric": 10}, lan), "unknown")

    def test_power_change_targets_one_index(self):
        with patch("engine.network.run_network_command", return_value=Mock(returncode=0)) as runner:
            set_adapter_enabled(10, False)
            command = runner.call_args.args[0]
            self.assertIn("$_.ifIndex -eq 10", command)
            self.assertIn("$adapter | Disable-NetAdapter -Confirm:$false -EA Stop", command)
            set_adapter_enabled(5, True)
            self.assertIn("Enable-NetAdapter", runner.call_args.args[0])

    def test_elevation_keeps_exit_code_and_hidden_window(self):
        with patch("engine.network.ctypes.windll.shell32.IsUserAnAdmin", return_value=0), patch("engine.network.subprocess.run") as runner:
            run_network_command("Write-Output 'ok'")
            command = runner.call_args.args[0][-1]
            self.assertIn("-WindowStyle Hidden", command)
            self.assertIn("exit $p.ExitCode", command)

    def test_parser_keeps_disabled_adapters_and_automatic_metric(self):
        data = [{"InterfaceAlias": "Wi-Fi", "InterfaceIndex": 10, "InterfaceMetric": 10,
                 "AutomaticMetric": "Disabled", "ConnectionState": "Up", "Enabled": True},
                {"InterfaceAlias": "Ethernet", "InterfaceIndex": 5, "InterfaceMetric": 50,
                 "AutomaticMetric": "Enabled", "ConnectionState": "Disabled", "Enabled": False}]
        with patch("engine.network.run_powershell", return_value=json.dumps(data)):
            adapters = get_network_interfaces()
        self.assertFalse(adapters["Wi-Fi"]["automatic_metric"])
        self.assertTrue(adapters["Ethernet"]["automatic_metric"])
        self.assertFalse(adapters["Ethernet"]["enabled"])


if __name__ == "__main__":
    unittest.main()
