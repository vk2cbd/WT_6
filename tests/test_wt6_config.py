from pathlib import Path
import tempfile
import unittest

from wt6_config import (
    B210Calibration,
    ScanConfig,
    YFactorConfig,
    load_b210_calibration,
    load_configs,
    load_scan_config,
    load_site_config,
    load_yfactor_config,
    save_b210_calibration,
    save_scan_config,
    save_yfactor_config,
)


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


    def test_yfactor_workflow_defaults_to_alternate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wt6_ubuntu.ini"
            path.write_text("[yfactor]\nantenna_name = East\n", encoding="utf-8")
            yfactor = load_yfactor_config(path)
        self.assertTrue(yfactor.alternate_order)

    def test_yfactor_workflow_save_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wt6_ubuntu.ini"
            save_yfactor_config(
                path,
                YFactorConfig(
                    antenna_name="West",
                    hot_target="Moon",
                    cold_mode="Moon AZ / EL 80",
                    count=4,
                    dwell_seconds=2.0,
                    alternate_order=False,
                ),
            )
            yfactor = load_yfactor_config(path)
        self.assertFalse(yfactor.alternate_order)
        self.assertEqual(yfactor.antenna_name, "West")

    def test_b210_calibration_save_load_is_per_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wt6_ubuntu.ini"
            save_b210_calibration(
                path,
                B210Calibration(1_150_000_000, 512_000, 512_000, "55", "55", "A", {-40: -17.0, -50: -27.0}),
            )
            save_b210_calibration(
                path,
                B210Calibration(1_150_000_000, 512_000, 512_000, "55", "55", "B", {-40: -20.0, -50: -30.0}),
            )
            cal_a = load_b210_calibration(path, 1_150_000_000, 512_000, 512_000, "55.0", "55", "A")
            cal_b = load_b210_calibration(path, 1_150_000_000, 512_000, 512_000, "55", "55.0", "B")
        self.assertEqual(cal_a.points_dbfs_by_dbm[-40], -17.0)
        self.assertEqual(cal_b.points_dbfs_by_dbm[-40], -20.0)


if __name__ == "__main__":
    unittest.main()



