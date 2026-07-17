import unittest

from wt6_b210_power import B210PowerMeterConfig, normalize_clock_source, parse_device_args


class B210PowerConfigTests(unittest.TestCase):
    def test_device_args_and_clock_normalization(self):
        self.assertEqual(parse_device_args("num_recv_frames=256, serial=abc"), {"num_recv_frames": "256", "serial": "abc"})
        self.assertEqual(normalize_clock_source("Int"), "internal")
        self.assertEqual(normalize_clock_source("EXT"), "external")

    def test_bandwidth_must_not_exceed_sample_rate(self):
        config = B210PowerMeterConfig(sample_rate_hz=1_024_000, measurement_bandwidth_hz=2_000_000)
        with self.assertRaises(ValueError):
            config.validate()


if __name__ == "__main__":
    unittest.main()
