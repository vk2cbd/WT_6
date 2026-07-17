from pathlib import Path
import tempfile
import unittest

from wt6_config import ScanConfig, load_configs, load_scan_config, load_site_config, save_scan_config


class ConfigEncodingTests(unittest.TestCase):
    def test_bom_marked_ini_loads(self):
        content = (
            "[site]\n"
            "latitude = -32.724\n"
            "longitude = 152.130167\n"
            "\n"
            "[antenna:East]\n"
            "port = /dev/ttyUSB0\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wt6_ubuntu.ini"
            path.write_text(content, encoding="utf-8-sig")
            site = load_site_config(path)
            configs = load_configs(path)
        self.assertAlmostEqual(site.latitude, -32.724)
        self.assertIn("East", configs)
        self.assertEqual(configs["East"].port, "/dev/ttyUSB0")

    def test_scan_direction_defaults_high_to_low(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wt6_ubuntu.ini"
            path.write_text("[scan]\nantenna_name = East\n", encoding="utf-8")
            scan = load_scan_config(path)
        self.assertTrue(scan.az_scan_high_to_low)

    def test_scan_direction_save_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wt6_ubuntu.ini"
            save_scan_config(
                path,
                ScanConfig(
                    span_degrees=4.0,
                    increment_degrees=0.5,
                    dwell_seconds=1.0,
                    scan_count=1,
                    antenna_name="West",
                    az_scan_high_to_low=False,
                ),
            )
            scan = load_scan_config(path)
        self.assertFalse(scan.az_scan_high_to_low)
        self.assertEqual(scan.antenna_name, "West")


if __name__ == "__main__":
    unittest.main()



