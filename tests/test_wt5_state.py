import unittest

from wt5_state import AppStateStore, AntennaRunState, PowerRunState, SystemRunState, antenna_state_from_text


class AppStateStoreTests(unittest.TestCase):
    def test_state_store_tracks_antenna_target_power_and_status(self):
        store = AppStateStore()
        store.set_status("Tracking Sun.", SystemRunState.TRACKING)
        store.set_target("Sun", 12.3, 45.6, "HA +01:02")
        store.set_antenna_state("East", AntennaRunState.TRACKING)
        store.set_antenna_position("East", 12.1, 45.4)
        store.set_power(PowerRunState.READY, value=-42.0, unit="dBm", calibrated=True, message="Ready CAL")

        snapshot = store.snapshot()
        self.assertEqual(snapshot.system_state, SystemRunState.TRACKING)
        self.assertEqual(snapshot.target.name, "Sun")
        self.assertAlmostEqual(snapshot.antennas["East"].azimuth, 12.1)
        self.assertEqual(snapshot.antennas["East"].run_state, AntennaRunState.TRACKING)
        self.assertEqual(snapshot.power.unit, "dBm")
        self.assertTrue(snapshot.power.calibrated)

    def test_reset_power_clears_previous_measurement(self):
        store = AppStateStore()
        store.set_power(PowerRunState.READY, value=-42.0, unit="dBm", calibrated=True, message="Ready")
        store.reset_power()
        snapshot = store.snapshot()
        self.assertEqual(snapshot.power.run_state, PowerRunState.STOPPED)
        self.assertIsNone(snapshot.power.value)
        self.assertEqual(snapshot.power.unit, "dBFS")

    def test_unknown_calibration_status_maps_to_tracking(self):
        self.assertEqual(antenna_state_from_text("CAL AZ"), AntennaRunState.TRACKING)
        self.assertEqual(antenna_state_from_text("CAL EL"), AntennaRunState.TRACKING)


if __name__ == "__main__":
    unittest.main()

