import unittest

from wt6_b210_power import (
    B210PowerMeterConfig,
    activate_b210_stream_with_timed_start,
    normalize_clock_source,
    parse_device_args,
)


class B210PowerConfigTests(unittest.TestCase):
    def test_device_args_and_clock_normalization(self):
        self.assertEqual(parse_device_args("num_recv_frames=256, serial=abc"), {"num_recv_frames": "256", "serial": "abc"})
        self.assertEqual(normalize_clock_source("Int"), "internal")
        self.assertEqual(normalize_clock_source("EXT"), "external")

    def test_bandwidth_must_not_exceed_sample_rate(self):
        config = B210PowerMeterConfig(sample_rate_hz=1_024_000, measurement_bandwidth_hz=2_000_000)
        with self.assertRaises(ValueError):
            config.validate()

    def test_timed_activation_is_used_for_dual_channel_stream(self):
        calls = []

        class FakeSdr:
            def setHardwareTime(self, value):
                calls.append(("setHardwareTime", value))

            def getHardwareTime(self):
                calls.append(("getHardwareTime", None))
                return 5_000

            def activateStream(self, stream, flags=0, timeNs=0):
                calls.append(("activateStream", stream, flags, timeNs))

        activate_b210_stream_with_timed_start(FakeSdr(), "rx", 123)
        self.assertEqual(calls[0], ("setHardwareTime", 0))
        self.assertEqual(calls[1], ("getHardwareTime", None))
        self.assertEqual(calls[2], ("activateStream", "rx", 123, 100_005_000))


class B210PowerPanelRoutingTests(unittest.TestCase):
    def test_default_west_antenna_uses_channel_b(self):
        from wt6_ubuntu_gui import PowerMeterPanel

        panel = PowerMeterPanel.__new__(PowerMeterPanel)
        self.assertEqual(panel.power_channel_for_antenna("East"), "A")
        self.assertEqual(panel.power_channel_for_antenna("West"), "B")
        self.assertEqual(panel.power_channel_for_antenna(""), "A")

    def test_channel_mapping_can_be_changed_in_config(self):
        from wt6_config import PowerConfig
        from wt6_ubuntu_gui import PowerMeterPanel

        class DummyApp:
            power_config = PowerConfig(east_channel="B", west_channel="A")

        panel = PowerMeterPanel.__new__(PowerMeterPanel)
        panel.app = DummyApp()
        self.assertEqual(panel.power_channel_for_antenna("East"), "B")
        self.assertEqual(panel.power_channel_for_antenna("West"), "A")

    def test_west_measurement_uses_channel_b_calibration(self):
        from wt6_config import B210Calibration, PowerConfig
        from wt6_ubuntu_gui import PowerMeterPanel

        class DummyApp:
            power_config = PowerConfig()

        panel = PowerMeterPanel.__new__(PowerMeterPanel)
        panel.app = DummyApp()
        panel.power_started_at = 0.0
        panel.latest_power_dbfs = -30.0
        panel.latest_power_b_dbfs = -25.0
        panel.active_calibrations = {
            "A": B210Calibration(1, 1, 1, "55", "55", "A", {-40: -20.0, -50: -30.0}),
            "B": B210Calibration(1, 1, 1, "55", "55", "B", {-40: -15.0, -50: -25.0}),
        }
        measurement = panel.current_power_measurement("West")
        self.assertEqual(measurement["power_channel"], "B")
        self.assertEqual(measurement["power_unit"], "dBm")
        self.assertAlmostEqual(measurement["power_value"], -50.0)


if __name__ == "__main__":
    unittest.main()
