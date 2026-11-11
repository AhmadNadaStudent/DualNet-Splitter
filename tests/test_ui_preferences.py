import json
import tempfile
import unittest
from pathlib import Path

from ui_preferences import display_settings, save_settings, translate


class PreferenceTests(unittest.TestCase):
    def test_preferences_preserve_tutorial_and_unknown_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ui.json"
            path.write_text(json.dumps({"tutorial_seen": True, "future_option": 42}), encoding="utf-8")
            self.assertTrue(save_settings(path, theme="dark", language="en", compact=True, compact_topmost=True))
            self.assertEqual(display_settings(path), {"theme": "dark", "language": "en", "compact": True,
                                                      "compact_topmost": True})
            settings = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(settings["tutorial_seen"])
            self.assertEqual(settings["future_option"], 42)
            self.assertTrue(save_settings(path, tutorial_seen=True))
            self.assertEqual(display_settings(path)["theme"], "dark")

    def test_invalid_settings_use_safe_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ui.json"
            for content in ('[]', 'null', '{broken', '{"theme":"other","language":"fr","compact":"false"}',
                            '{"theme":[],"language":{},"compact":1,"compact_topmost":"true"}'):
                path.write_text(content, encoding="utf-8")
                self.assertEqual(display_settings(path), {"theme": "light", "language": "id", "compact": False,
                                                          "compact_topmost": False})

    def test_dynamic_feedback_can_be_retranslated_and_preserves_diagnostics(self):
        message = translate("Gagal: {v0}", "en", v0="Windows {raw error}")
        self.assertEqual(message, "Failed: Windows {raw error}")
        self.assertEqual(translate(message, "id"), "Gagal: Windows {raw error}")
        self.assertEqual(translate("Validasi config TUN gagal: {raw error}", "en"),
                         "TUN configuration validation failed: {raw error}")


if __name__ == "__main__":
    unittest.main()
