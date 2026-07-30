import os
import shutil
import tempfile
import unittest

from procwatch import config, db, power

# Real lines from `pmset -g log` on a Mac. Parsing is asserted against these
# rather than against invented shapes, because the format is the one thing
# here that nobody documents and that changes between releases.
LOG = """\
2026-07-28 23:14:02 -0700 Sleep               \tEntering Sleep state due to 'Clamshell Sleep':TCPKeepAlive=active Using Batt (Charge:98%) 41 secs
2026-07-29 02:30:11 -0700 DarkWake            \tDarkWake from Deep Idle [CDNVA] : due to smc.sysState.Wake(0x70070000) wifibt SMC.OutboxNotEmpty/ Using BATT (Charge:91%)
2026-07-29 02:31:44 -0700 Sleep               \tEntering Sleep state due to 'Maintenance Sleep':TCPKeepAlive=active Using Batt (Charge:91%) 93 secs
2026-07-29 07:58:20 -0700 Wake                \tWake from Deep Idle [CDNVA] : due to smc.sysState.Wake(0x70070000) lid RTP.multi-touch/UserActivity Assertion Using BATT (Charge:74%)
2026-07-29 09:02:00 -0700 Assertions          \tPID 500(cloudd) Released SystemIsActive "task" 00:00:04  id:0x0xc000084ca
"""


class PowerCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.real = config.DB_PATH
        config.DB_PATH = os.path.join(self.dir, "t.db")
        self.conn = db.connect(config.DB_PATH)
        db.init_schema(self.conn)
        power.init(self.conn)

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self.real
        shutil.rmtree(self.dir, ignore_errors=True)


class TestParsing(PowerCase):
    def test_it_reads_sleeps_and_wakes_and_ignores_the_rest(self):
        # The log is 99% assertion lines; importing them would make reading it
        # pointless, since assertions are observed live and far more cheaply.
        self.assertEqual(power.import_log(self.conn, now=1, text=LOG), 4)

    def test_a_timestamp_keeps_its_own_offset(self):
        """The offset is in the line, so it is never inferred.

        Inferring it is where the daylight-saving bug lives: a local time fed
        to something that has to guess the offset lands an hour out for half
        the year.
        """
        self.assertEqual(power._stamp("2026-07-29 02:30:11 -0700"), 1785317411)
        # An offset that is NOT this machine's, so the test fails if the
        # offset is inferred from the local zone rather than read from the
        # line. With a local-time parse these two land on the same instant,
        # which is exactly the bug.
        self.assertEqual(power._stamp("2026-07-29 02:30:11 +0000"), 1785292211)
        self.assertNotEqual(power._stamp("2026-07-29 02:30:11 +0000"),
                            power._stamp("2026-07-29 02:30:11 -0700"))
        # A log written in India, read on a Mac in California.
        self.assertEqual(power._stamp("2026-07-29 02:30:11 +0530"), 1785272411)

    def test_hours_are_not_wrapped_at_a_day(self):
        # cupsd had held an assertion for 115 hours on the machine this was
        # written on; treating that as 19:19 would report it as fine.
        self.assertEqual(power._seconds("115:19:14"), 115 * 3600 + 19 * 60 + 14)
        self.assertGreater(power._seconds("115:19:14"), 4 * 86400)
        self.assertEqual(power._seconds("00:00:04"), 4)

    def test_charge_is_read_in_both_shapes_the_log_uses(self):
        # "Using Batt (Charge:98%)" and "Using AC(Charge: 61)" both occur.
        power.import_log(self.conn, now=1, text=LOG)
        rows = dict(self.conn.execute(
            "SELECT kind, charge FROM power_event ORDER BY ts").fetchall())
        self.assertEqual(rows["wake"], 74)
        self.assertIn(rows["darkwake"], (91,))

    def test_reimporting_the_same_window_changes_nothing(self):
        power.import_log(self.conn, now=1, text=LOG)
        first = self.conn.execute("SELECT COUNT(*) FROM power_event").fetchone()[0]
        power.import_log(self.conn, now=2, text=LOG)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM power_event").fetchone()[0],
            first)


class TestPlainLanguage(PowerCase):
    """A driver string is the answer and not an answer anybody can read."""

    def test_a_lid_beats_the_hardware_that_also_appears(self):
        # Every wake line names the SMC. Reporting that would answer every
        # question with "the hardware controller".
        self.assertEqual(
            power.describe_wake(
                "due to smc.sysState.Wake(0x70070000) lid RTP.multi-touch/UserActivity"),
            "you opened the lid")

    def test_wifi_and_bluetooth_are_named(self):
        # 83 of 149 wakes on the machine this was written on. Left unmatched
        # they read as "unknown", which is the least useful true answer.
        self.assertEqual(
            power.describe_wake("smc.sysState.Wake(0x70070000) wifibt SMC.OutboxNotEmpty/"),
            "Wi-Fi or Bluetooth")
        self.assertEqual(
            power.describe_wake("wifibt SMC.OutboxNotEmpty bluetooth-pcie/"),
            "a Bluetooth device")

    def test_an_unrecognised_reason_says_so_rather_than_guessing(self):
        self.assertEqual(power.describe_wake("qqq"), "an unknown cause")

    def test_sleep_reasons_are_translated_too(self):
        self.assertEqual(power.describe_sleep("Clamshell Sleep"), "the lid closing")
        self.assertEqual(power.describe_sleep("Maintenance Sleep"), "a maintenance nap")


class TestHolds(PowerCase):
    def _tick(self, holds, now):
        real = power.read_holds
        power.read_holds = lambda: holds
        try:
            power.tick(self.conn, now=now)
        finally:
            power.read_holds = real

    def _hold(self, aid, process, seconds, kind="PreventUserIdleSystemSleep"):
        return {"aid": aid, "pid": 1, "process": process, "kind": kind,
                "name": "", "seconds": seconds}

    def test_a_hold_is_recorded_with_the_duration_pmset_reports(self):
        """Not one derived from when we happened to look.

        The recorder can miss ticks -- the machine sleeps. pmset knows how long
        the hold has really lasted, so that number is stored.
        """
        self._tick([self._hold("0x1", "Claude", 118_000)], now=1_000_000)
        row = self.conn.execute(
            "SELECT process, seconds, first_ts FROM power_hold").fetchone()
        self.assertEqual(row[0], "Claude")
        self.assertEqual(row[1], 118_000)
        self.assertEqual(row[2], 1_000_000 - 118_000)

    def test_a_hold_that_disappears_is_closed(self):
        self._tick([self._hold("0x1", "Claude", 60)], now=1000)
        self._tick([], now=1100)
        self.assertEqual(
            self.conn.execute("SELECT open FROM power_hold").fetchone()[0], 0)

    def test_a_continuing_hold_is_the_same_row(self):
        # Keyed by the assertion id, which is unique to that hold.
        self._tick([self._hold("0x1", "Claude", 60)], now=1000)
        self._tick([self._hold("0x1", "Claude", 160)], now=1100)
        rows = self.conn.execute("SELECT COUNT(*), MAX(seconds) FROM power_hold").fetchone()
        self.assertEqual(rows, (1, 160))

    def test_only_holds_that_stop_sleep_are_blamed(self):
        # A display assertion keeps the screen on, which is usually the person
        # sitting there. Reporting it as keeping the Mac awake is wrong.
        self._tick([self._hold("0x1", "caffeinate", 3600),
                    self._hold("0x2", "caffeinate", 3600,
                               kind="PreventUserIdleDisplaySleep")], now=10_000)
        blamed = power.kept_awake(self.conn, 0, 20_000)
        self.assertEqual(len(blamed), 1)
        self.assertEqual(blamed[0]["seconds"], 3600)

    def test_the_window_only_counts_the_part_inside_it(self):
        """A five-day hold must not report five days against last night."""
        self._tick([self._hold("0x1", "cupsd", 5 * 86400)], now=1_000_000)
        night = power.kept_awake(self.conn, 1_000_000 - 3600, 1_000_000)
        self.assertEqual(night[0]["seconds"], 3600)

    def test_what_you_are_doing_yourself_is_not_reported(self):
        real = power._HOLD
        line = ('   pid 647(WindowServer): [0x0005] 00:03:55 UserIsActive '
                'named: "queue"')
        another = ('   pid 46015(Claude): [0x0006] 32:46:29 NoIdleSleepAssertion '
                   'named: "Electron"')
        import subprocess
        done = subprocess.CompletedProcess([], 0, line + "\n" + another, "")
        original = subprocess.run
        subprocess.run = lambda *a, **k: done
        try:
            holds = power.read_holds()
        finally:
            subprocess.run = original
        self.assertEqual([h["process"] for h in holds], ["Claude"])


class TestDrain(PowerCase):
    def test_it_reports_what_the_lid_being_shut_cost(self):
        power.import_log(self.conn, now=1, text=LOG)
        drain = power.overnight_drain(self.conn, 0, 2_000_000_000)
        self.assertIsNotNone(drain)
        # 98 -> 91 across the first sleep, 91 -> 74 across the second.
        self.assertEqual(drain["charge_lost"], 7 + 17)
        self.assertEqual(drain["wakes"], 2)

    def test_losing_charge_on_mains_is_not_reported_as_drain(self):
        """A different fault, and reporting it here buries the one people mean."""
        on_ac = LOG.replace("Using Batt (Charge:98%)", "Using AC(Charge: 98)")
        power.import_log(self.conn, now=1, text=on_ac)
        drain = power.overnight_drain(self.conn, 0, 2_000_000_000)
        self.assertEqual(drain["wakes"], 1)   # only the battery-powered span

    def test_a_machine_that_never_slept_reports_nothing(self):
        self.assertIsNone(power.overnight_drain(self.conn, 0, 2_000_000_000))


if __name__ == "__main__":
    unittest.main()
