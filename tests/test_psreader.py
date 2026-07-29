# tests/test_psreader.py
import os
import time
import unittest
from unittest import mock

from procwatch import psreader

MAIN = """\
  PID STARTED                          TIME  RSS COMM
    1 Fri Jul 24 22:17:35 2026      0:50.88 2288 /sbin/launchd
  647 Fri Jul 24 22:18:58 2026     40:08.70 121936 /System/Library/PrivateFrameworks/SkyLight.framework/Resources/WindowServer
 5976 Sun Jul 27 14:24:02 2026   2:03:04.50 138144 /Users/you/Desktop/Notes.app/Contents/MacOS/Notes
"""

CMDS = """\
  PID COMMAND
    1 /sbin/launchd
  647 /System/Library/PrivateFrameworks/SkyLight.framework/Resources/WindowServer
 5976 /Users/you/Desktop/Notes.app/Contents/MacOS/Notes --verbose
"""


class TestParsing(unittest.TestCase):
    def test_cputime_minutes_and_seconds(self):
        self.assertEqual(psreader.parse_cputime("0:50.88"), 5088)

    def test_cputime_with_hours(self):
        self.assertEqual(psreader.parse_cputime("2:03:04.50"), (2 * 3600 + 3 * 60 + 4) * 100 + 50)

    def test_lstart_round_trips_to_a_stable_integer(self):
        a = psreader.parse_lstart("Fri Jul 24 22:17:35 2026")
        b = psreader.parse_lstart("Fri Jul 24 22:17:35 2026")
        self.assertEqual(a, b)
        self.assertGreater(a, 0)

    def test_lstart_tolerates_space_padded_days(self):
        self.assertGreater(psreader.parse_lstart("Fri Jul  4 22:17:35 2026"), 0)


class TestCombine(unittest.TestCase):
    def setUp(self):
        self.procs = {p.pid: p for p in psreader.combine(MAIN, CMDS)}

    def test_lstart_five_tokens_do_not_shift_later_columns(self):
        # A whitespace split would land RSS in the TIME column.
        self.assertEqual(self.procs[647].cputime_cs, (40 * 60 + 8) * 100 + 70)
        self.assertEqual(self.procs[647].rss_kb, 121936)

    def test_comm_keeps_its_full_path_including_spaces(self):
        self.assertTrue(self.procs[647].comm.endswith("WindowServer"))

    def test_command_comes_from_the_second_call(self):
        self.assertEqual(
            self.procs[5976].command,
            "/Users/you/Desktop/Notes.app/Contents/MacOS/Notes --verbose")

    def test_a_pid_missing_from_the_command_call_falls_back_to_comm(self):
        procs = {p.pid: p for p in psreader.combine(MAIN, "  PID COMMAND\n    1 /sbin/launchd\n")}
        self.assertEqual(procs[647].command, procs[647].comm)

    def test_unexpected_header_is_rejected(self):
        # ps drops a bad keyword and still emits the rest; trusting the
        # requested columns rather than the returned ones misreads every row.
        bad = MAIN.replace("STARTED", "ELAPSED")
        with self.assertRaises(psreader.PsError):
            list(psreader.combine(bad, CMDS))

    def test_a_failing_command_call_raises_rather_than_degrading_silently(self):
        ok = mock.Mock(returncode=0, stdout=MAIN, stderr="")
        bad = mock.Mock(returncode=1, stdout="  PID COMMAND\n", stderr="ps: boom")
        with mock.patch("procwatch.psreader.subprocess.run", side_effect=[ok, bad]):
            with self.assertRaises(psreader.PsError):
                psreader.read()


class TestLstartAcrossDST(unittest.TestCase):
    """Pin the timezone so these absolute epochs mean something."""

    def setUp(self):
        self._saved = os.environ.get("TZ")
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._saved
        time.tzset()

    def test_a_summer_timestamp_uses_the_daylight_offset(self):
        # 22:17:35 PDT on 24 Jul 2026 is UTC-7. Computing the offset from
        # time.timezone instead would give 1784960255 -- an hour late.
        self.assertEqual(psreader.parse_lstart("Fri Jul 24 22:17:35 2026"), 1784956655)

    def test_a_winter_timestamp_uses_the_standard_offset(self):
        # 22:17:35 PST on 24 Jan 2026 is UTC-8.
        self.assertEqual(psreader.parse_lstart("Sat Jan 24 22:17:35 2026"), 1769321855)


if __name__ == "__main__":
    unittest.main()
