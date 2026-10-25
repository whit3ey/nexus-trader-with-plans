import unittest
from bot.hardware import (Hardware, parse_hardware_profile, parse_gpu_cores, select_model, format_report,
                          MAX_MODEL_FRACTION)

SAMPLE = """Hardware:

    Hardware Overview:

      Model Name: MacBook Pro
      Model Identifier: Mac15,7
      Chip: Apple M3 Pro
      Total Number of Cores: 12 (6 performance and 6 efficiency)
      Memory: 36 GB
      System Firmware Version: 10151.101.3
"""
GPU = """Graphics/Displays:

    Apple M3 Pro:

      Chipset Model: Apple M3 Pro
      Type: GPU
      Bus: Built-In
      Total Number of Cores: 18
"""


class HardwareTests(unittest.TestCase):
    def test_parse_profile(self):
        p = parse_hardware_profile(SAMPLE)
        self.assertEqual(p["chip"], "Apple M3 Pro")
        self.assertEqual(p["memory_gb"], 36.0)
        self.assertEqual(p["cpu_cores"], 12)
        self.assertEqual(p["cpu_detail"], "6 performance and 6 efficiency")
        self.assertEqual(p["model_name"], "MacBook Pro")

    def test_parse_gpu(self):
        self.assertEqual(parse_gpu_cores(GPU), 18)
        self.assertIsNone(parse_gpu_cores("nothing"))

    def test_memory_tb_and_garbage(self):
        self.assertEqual(parse_hardware_profile("Memory: 1 TB")["memory_gb"], 1024.0)
        self.assertEqual(parse_hardware_profile("garbage"), {})

    def test_model_by_ram_tier(self):
        self.assertEqual(select_model(8).model, "qwen3:4b")
        self.assertEqual(select_model(16).model, "qwen3:14b")
        self.assertEqual(select_model(32).model, "qwen3:30b")
        self.assertEqual(select_model(64).model, "qwen3:30b")
        self.assertEqual(select_model(128).model, "qwen3:30b")

    def test_model_never_exceeds_memory_fraction(self):
        for gb in (4, 6, 8, 12, 16, 18, 24, 32, 36, 48, 64, 96):
            c = select_model(gb)
            self.assertLessEqual(c.size_gb, gb * MAX_MODEL_FRACTION + 1e-9, (gb, c))

    def test_low_memory_and_unknown_memory_choose_small_models(self):
        self.assertEqual(select_model(4).model, "qwen3:4b" if 2.6 <= 4 * 0.65 else "qwen3:1.7b")
        self.assertEqual(select_model(0).model, "qwen3:4b")

    def test_override_wins_and_auto_is_not_an_override(self):
        self.assertEqual(select_model(8, override="qwen3:8b").model, "qwen3:8b")
        self.assertIn("manually", select_model(8, override="qwen3:8b").reason)
        self.assertEqual(select_model(16, override="auto").model, "qwen3:14b")
        self.assertEqual(select_model(16, override="").model, "qwen3:14b")

    def test_needs_pull_reflects_installed_models(self):
        self.assertFalse(select_model(16, installed=["qwen3:14b"]).needs_pull)
        self.assertFalse(select_model(16, installed=["qwen3:14b:latest"]).needs_pull)
        self.assertTrue(select_model(16, installed=["llama3:8b"]).needs_pull)

    def test_report_contains_required_labels(self):
        hw = Hardware("Apple M3 Pro", 36, 12, "6P+6E", 18, "15.0", "arm64", True)
        text = format_report(hw, select_model(36))
        for label in ("Detected hardware:", "Chip:", "Memory:", "CPU:", "GPU:", "Selected AI model:",
                      "Estimated model size:", "Reason for selection:"):
            self.assertIn(label, text)


if __name__ == "__main__":
    unittest.main()
