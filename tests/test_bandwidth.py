import ctypes
import unittest

from engine.bandwidth import BandwidthSampler, InterfaceCounters, _MIB_IF_ROW2, format_rate


class BandwidthTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.rows = {10: InterfaceCounters(1, 5_000_000_000, 1000),
                     5: InterfaceCounters(2, 1000, 2000)}
        self.sampler = BandwidthSampler(self.rows.get, lambda: self.now)
        self.wifi = {"state": "Up", "index": 10}
        self.lan = {"state": "Up", "index": 5}

    def test_separate_adapters_and_real_elapsed_time(self):
        self.assertIsNone(self.sampler.sample("wifi", self.wifi))
        self.assertIsNone(self.sampler.sample("lan", self.lan))
        self.now += 2.5
        self.rows[10] = InterfaceCounters(1, 5_002_500_000, 251_000)
        self.rows[5] = InterfaceCounters(2, 51_000, 4500)
        self.assertEqual(self.sampler.sample("wifi", self.wifi), (1_000_000, 100_000))
        self.assertEqual(self.sampler.sample("lan", self.lan), (20_000, 1000))

    def test_idle_is_zero(self):
        self.sampler.sample("wifi", self.wifi)
        self.now += 1
        self.assertEqual(self.sampler.sample("wifi", self.wifi), (0, 0))

    def test_reset_does_not_spike_or_go_negative(self):
        self.sampler.sample("wifi", self.wifi)
        self.now += 1
        self.rows[10] = InterfaceCounters(1, 10, 10)
        self.assertIsNone(self.sampler.sample("wifi", self.wifi))
        self.now += 1
        self.rows[10] = InterfaceCounters(1, 30, 20)
        self.assertEqual(self.sampler.sample("wifi", self.wifi), (20, 10))

    def test_reconnect_rebases(self):
        self.sampler.sample("wifi", self.wifi)
        self.assertIsNone(self.sampler.sample("wifi", {"state": "Disconnected", "index": 10}))
        self.now += 100
        self.rows[10] = InterfaceCounters(1, 6_000_000_000, 1_000_000)
        self.assertIsNone(self.sampler.sample("wifi", self.wifi))

    def test_reused_index_rebases(self):
        self.sampler.sample("wifi", self.wifi)
        self.now += 1
        self.rows[10] = InterfaceCounters(3, 9_000_000_000, 9_000_000)
        self.assertIsNone(self.sampler.sample("wifi", self.wifi))

    def test_missing_adapter_and_failed_read_clear_old_sample(self):
        self.sampler.sample("wifi", self.wifi)
        self.rows[10] = None
        self.assertIsNone(self.sampler.sample("wifi", self.wifi))
        self.assertNotIn("wifi", self.sampler.previous)
        self.assertIsNone(self.sampler.sample("lan", None))

    def test_native_struct_matches_windows_abi(self):
        self.assertEqual(ctypes.sizeof(_MIB_IF_ROW2), 1352)
        self.assertEqual(_MIB_IF_ROW2.InOctets.offset, 1208)
        self.assertEqual(_MIB_IF_ROW2.OutOctets.offset, 1280)

    def test_units(self):
        self.assertEqual(format_rate(0), "0.0 B/s")
        self.assertEqual(format_rate(125_000), "125.0 KB/s")
        self.assertEqual(format_rate(12_500_000), "12.5 MB/s")
        self.assertEqual(format_rate(2_500_000_000), "2.5 GB/s")


if __name__ == "__main__":
    unittest.main()
