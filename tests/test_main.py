# tests/test_main.py
import os
import tempfile
import unittest
from unittest import mock

from procwatch import main, psreader, system

PROCS = [psreader.Proc(1, 1000, 500, 2048, "/usr/bin/thing", "/usr/bin/thing")]
READINGS = system.Readings(100, 5000, 100, 0, 10 ** 9)


class TestRunOnce(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "t.db")
        self.log = os.path.join(self.dir, "t.log")
        patches = [
            mock.patch("procwatch.config.DB_PATH", self.db),
            mock.patch("procwatch.config.LOG_PATH", self.log),
            mock.patch("procwatch.system.read", return_value=READINGS),
            mock.patch("procwatch.system.free_bytes", return_value=10 ** 12),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_a_successful_tick_returns_zero_and_creates_the_database(self):
        with mock.patch("procwatch.psreader.read", return_value=PROCS):
            self.assertEqual(main.run_once(now=1000), 0)
        self.assertTrue(os.path.exists(self.db))

    def test_a_ps_failure_is_logged_and_does_not_raise(self):
        with mock.patch("procwatch.psreader.read",
                        side_effect=psreader.PsError("boom")):
            self.assertEqual(main.run_once(now=1000), 1)
        with open(self.log) as handle:
            self.assertIn("boom", handle.read())

    def test_a_system_read_failure_is_logged_and_skips_the_tick_without_touching_the_db(self):
        # system.read() raises SystemReadError rather than returning zeros; a
        # skipped tick must never reach the database, or a soft vm_stat
        # failure would write a fully-formed row with mem_used_kb=0 that
        # looks like real data and stays in the history permanently.
        with mock.patch("procwatch.psreader.read", return_value=PROCS), \
             mock.patch("procwatch.system.read",
                        side_effect=system.SystemReadError("vm_stat broke")):
            self.assertEqual(main.run_once(now=1000), 1)
        with open(self.log) as handle:
            self.assertIn("vm_stat broke", handle.read())
        self.assertFalse(os.path.exists(self.db))

    def test_a_write_failure_after_a_good_read_is_logged_and_does_not_raise(self):
        with mock.patch("procwatch.psreader.read", return_value=PROCS), \
             mock.patch("procwatch.sampler.tick",
                        side_effect=RuntimeError("kaboom")):
            self.assertEqual(main.run_once(now=1000), 1)
        with open(self.log) as handle:
            self.assertIn("kaboom", handle.read())

    def test_consecutive_ticks_produce_a_sample(self):
        with mock.patch("procwatch.psreader.read", return_value=PROCS):
            main.run_once(now=1000)
        later = [PROCS[0]._replace(cputime_cs=800)]
        with mock.patch("procwatch.psreader.read", return_value=later):
            main.run_once(now=1030)
        from procwatch import db
        conn = db.connect(self.db)
        count = conn.execute("SELECT COUNT(*) FROM sample_raw").fetchone()[0]
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
