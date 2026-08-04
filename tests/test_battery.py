"""Battery condition, parsed out of ioreg.

The one that matters is health. On Apple silicon "MaxCapacity" reports 100
forever -- it is a percentage of the *current* full charge, not of the
original -- so a tool that reads it tells every user their three-year-old
battery is perfect. These pin the real comparison, and pin the parsing against
ioreg's habit of printing nested dictionaries inline.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from procwatch import battery

# Trimmed from this machine's real output, including the inline nesting that
# breaks a naive "find the key, take the digits" parser.
SAMPLE = '''
    "BatteryInstalled" = Yes
    "CycleCount" = 179
    "DesignCycleCount9C" = 1000
    "ExternalConnected" = Yes
    "IsCharging" = Yes
    "CurrentCapacity" = 80
    "Voltage" = 12501
    "InstantAmperage" = 1210
    "AvgTimeToFull" = 91
    "AvgTimeToEmpty" = 65535
    "Serial" = "F8Y441309R413QFCW"
    "Temperature" = 3012
    "BatteryData" = {"DesignCapacity"=4563,"CurrentCapacity"=80,"BatteryPower"=15126}
    "NominalChargeCapacity" = 3920
    "MaxCapacity" = 100
    "FullyCharged" = 0
'''


class BatteryParseTest(unittest.TestCase):
    def setUp(self):
        self.state = battery.parse(SAMPLE)

    def test_it_is_present(self):
        self.assertTrue(self.state["present"])

    def test_health_is_nominal_over_design(self):
        """NOT MaxCapacity, which says 100 on a worn battery."""
        self.assertAlmostEqual(self.state["health"], 85.9, places=1)

    def test_max_capacity_is_not_mistaken_for_health(self):
        self.assertNotEqual(self.state["health"], 100)

    def test_an_inline_nested_value_is_read_correctly(self):
        """DesignCapacity is printed inside BatteryData with no spaces:
        "DesignCapacity"=4563,"CurrentCapacity"=80. A parser that grabs
        trailing digits reads 456380."""
        self.assertEqual(self.state["design_mah"], 4563)

    def test_cycles_and_what_is_left(self):
        self.assertEqual(self.state["cycles"], 179)
        self.assertEqual(self.state["rated_cycles"], 1000)
        self.assertEqual(self.state["cycles_left"], 821)

    def test_charging_state(self):
        self.assertTrue(self.state["charging"])
        self.assertTrue(self.state["plugged"])
        self.assertFalse(self.state["fully_charged"])

    def test_the_unusable_estimate_is_dropped(self):
        """65535 is ioreg's "cannot say", not eleven centuries."""
        self.assertEqual(self.state["to_full_minutes"], 91)
        self.assertIsNone(self.state["to_empty_minutes"])

    def test_this_battery_is_not_flagged_worn(self):
        self.assertFalse(self.state["worn"])

    def test_a_worn_battery_is_flagged(self):
        worn = battery.parse(SAMPLE.replace('"NominalChargeCapacity" = 3920',
                                            '"NominalChargeCapacity" = 3200'))
        self.assertTrue(worn["worn"])
        self.assertIn("Service Recommended", battery.verdict(worn))

    def test_no_battery_is_not_a_crash(self):
        self.assertEqual(battery.parse(""), {"present": False})
        self.assertEqual(battery.parse('"BatteryInstalled" = No'),
                         {"present": False})
        self.assertIn("No battery", battery.verdict({"present": False}))

    def test_the_verdict_is_a_sentence_not_a_number(self):
        said = battery.verdict(self.state)
        self.assertIn("86%", said)
        self.assertIn("179", said)


class BatteryLiveTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "macOS only")
    def test_it_reads_this_machine_without_raising(self):
        state = battery.read()
        self.assertIn("present", state)
        if state["present"]:
            # Sanity, not exact values: this runs on whatever Mac it runs on.
            self.assertTrue(0 <= (state["percent"] or 0) <= 100)
            if state["health"] is not None:
                self.assertTrue(0 < state["health"] <= 110)


if __name__ == "__main__":
    unittest.main()
