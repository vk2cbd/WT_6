import unittest

from wt6_antenna import Direction, SafetyError, SafetyLimits


class SafetyLimitTests(unittest.TestCase):
    def test_wrap_dead_zone_uses_allowed_arc(self):
        limits = SafetyLimits(az_min=270.0, az_max=265.0, el_min=0.0, el_max=90.0)
        self.assertAlmostEqual(limits.azimuth_delta_to_target(240.0, 52.0), -188.0)

    def test_target_inside_dead_zone_is_rejected(self):
        limits = SafetyLimits(az_min=270.0, az_max=265.0, el_min=0.0, el_max=90.0)
        with self.assertRaises(SafetyError):
            limits.azimuth_delta_to_target(240.0, 267.0)

    def test_elevation_above_90_is_rejected(self):
        limits = SafetyLimits(az_min=270.0, az_max=265.0, el_min=0.0, el_max=90.0)
        with self.assertRaises(SafetyError):
            limits.assert_position_allowed(20.0, 91.0)

    def test_margin_stops_near_upper_elevation(self):
        limits = SafetyLimits(az_min=270.0, az_max=265.0, el_min=0.0, el_max=90.0, el_margin=0.5)
        with self.assertRaises(SafetyError):
            limits.assert_move_allowed(Direction.EL_UP, 20.0, 89.8)


if __name__ == "__main__":
    unittest.main()



