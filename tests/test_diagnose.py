import os
import shutil
import tempfile
import unittest

from procwatch import config, db, diagnose, power


class DiagnoseCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.real = config.DB_PATH
        config.DB_PATH = os.path.join(self.dir, "t.db")
        self.conn = db.connect(config.DB_PATH)
        db.init_schema(self.conn)
        power.init(self.conn)
        self.now = 1_000_000

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self.real
        shutil.rmtree(self.dir, ignore_errors=True)

    def _proc(self, exe, app="", is_system=0):
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO proc (exe, args_sig, cmdline_full, "
                "is_system, app) VALUES (?,'','',?,?)", (exe, is_system, app))
        return self.conn.execute(
            "SELECT id FROM proc WHERE exe = ? AND args_sig = ''", (exe,)).fetchone()[0]

    def _busy(self, exe, cpu, count, start=None, app="", rss_mb=100, disk=0,
              is_system=0):
        pid = self._proc(exe, app=app, is_system=is_system)
        start = self.now - count * config.INTERVAL if start is None else start
        with self.conn:
            for i in range(count):
                ts = start + i * config.INTERVAL
                self.conn.execute(
                    "INSERT OR REPLACE INTO sample_raw (ts, proc_id, cpu_avg, "
                    "cpu_max, cpu_max_ts, rss_avg, rss_max, nproc, samples, "
                    "disk_read, disk_write) VALUES (?,?,?,?,?,?,?,1,1,?,0)",
                    (ts, pid, int(cpu * 10), int(cpu * 10), ts,
                     int(rss_mb * 1024), int(rss_mb * 1024), disk))

    def _system(self, count, cpu=10.0, swap_kb=0, swap_end_kb=None, start=None):
        start = self.now - count * config.INTERVAL if start is None else start
        swap_end_kb = swap_kb if swap_end_kb is None else swap_end_kb
        with self.conn:
            for i in range(count):
                ts = start + i * config.INTERVAL
                swap = swap_kb + (swap_end_kb - swap_kb) * i // max(count - 1, 1)
                self.conn.execute(
                    "INSERT OR REPLACE INTO system_raw (ts, cpu_busy, load1, "
                    "mem_used_kb, mem_comp_kb, swap_used_kb, disk_free_kb, "
                    "samples, expected) VALUES (?,?,?,?,?,?,?,1,1)",
                    (ts, int(cpu * 10), 100, 8_000_000, 500_000, swap, 100_000_000))

    def _hold(self, process, kind, seconds, end=None):
        end = self.now if end is None else end
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO power_hold (aid, pid, process, kind, "
                "name, first_ts, last_ts, seconds, open) "
                "VALUES (?,1,?,?,'',?,?,?,1)",
                ("a%s%s" % (process, kind), process, kind,
                 end - seconds, end, seconds))

    def _explain(self, span=3600):
        return diagnose.explain(self.conn, self.now - span, self.now, now=self.now)

    def _kinds(self, result):
        return [f["kind"] for f in result["findings"]]


class TestSayingNothing(DiagnoseCase):
    """A diagnosis that always finds a culprit is a horoscope.

    These are the tests that keep it honest, and they matter more than the ones
    that find things: a tool nobody believes is worse than no tool.
    """

    def test_an_empty_window_says_nothing_was_recorded(self):
        result = self._explain()
        self.assertEqual(result["findings"], [])
        self.assertIn("Nothing was recorded", result["verdict"])

    def test_a_quiet_machine_is_told_it_was_fine(self):
        self._system(120, cpu=8.0)
        self._busy("Finder", 0.4, 120)
        result = self._explain()
        self.assertEqual(result["findings"], [])
        self.assertIn("fine", result["verdict"])

    def test_a_brief_spike_is_not_a_cause(self):
        # A compiler starting up. Four samples is two minutes.
        self._system(120, cpu=40.0)
        self._busy("swift-frontend", 300.0, 4)
        self.assertNotIn("busy-app", self._kinds(self._explain()))


class TestNamingTheCulprit(DiagnoseCase):
    def test_a_sustained_hog_is_the_cause_with_its_numbers(self):
        self._system(120, cpu=90.0)
        self._busy("Arc Helper", 180.0, 40, app="Arc")
        result = self._explain()
        found = [f for f in result["findings"] if f["kind"] == "busy-app"][0]
        self.assertIn("Arc", found["headline"])
        self.assertEqual(found["severity"], "cause")
        self.assertGreaterEqual(found["evidence"]["peak_cpu"], 180.0)
        self.assertGreaterEqual(found["evidence"]["seconds"], 4 * 60)
        self.assertIn("Arc", result["verdict"])

    def test_an_obscure_system_job_is_explained_in_english(self):
        """"mds_stores" means nothing to almost everyone.

        Translating it is most of the value of the whole feature -- the raw
        name is already in Activity Monitor and helps nobody.
        """
        self._system(120, cpu=70.0)
        self._busy("mds_stores", 120.0, 40, is_system=1)
        found = [f for f in self._explain()["findings"]
                 if f["kind"] == "system-work"][0]
        self.assertIn("Spotlight", found["headline"])
        self.assertIn("index", found["headline"])
        self.assertEqual(found["evidence"]["process"], "mds_stores")

    def test_a_system_job_that_finished_says_there_is_nothing_to_do(self):
        self._system(120, cpu=70.0)
        self._busy("backupd", 120.0, 30,
                   start=self.now - 3000, is_system=1)
        found = [f for f in self._explain()["findings"]
                 if f["kind"] == "system-work"][0]
        self.assertIn("Time Machine", found["headline"])
        self.assertIn("finished", found["detail"])
        self.assertIn("Nothing to do", found["advice"])

    def test_every_finding_carries_the_evidence_for_it(self):
        # A confident sentence with nothing under it is worse than a chart.
        self._system(120, cpu=90.0)
        self._busy("Arc Helper", 200.0, 40, app="Arc")
        for finding in self._explain()["findings"]:
            self.assertTrue(finding["evidence"],
                            "%s has no evidence" % finding["kind"])


class TestMemory(DiagnoseCase):
    def test_swap_growing_fast_is_a_cause(self):
        self._system(120, cpu=50.0, swap_kb=0, swap_end_kb=2_000_000)
        self._busy("Arc Helper", 5.0, 120, app="Arc", rss_mb=4000)
        found = [f for f in self._explain()["findings"]
                 if f["kind"] == "memory-pressure"]
        self.assertTrue(found)
        self.assertEqual(found[0]["severity"], "cause")

    def test_swap_creeping_over_a_long_window_is_not(self):
        """Ordinary use grows swap. A fixed threshold reports every long
        window as a memory problem, which trains people to ignore it."""
        day = 2880
        self._system(day, cpu=20.0, swap_kb=0, swap_end_kb=600_000,
                     start=self.now - 86400)
        self._busy("Arc Helper", 3.0, day, app="Arc", start=self.now - 86400)
        self.assertNotIn("memory-pressure", self._kinds(self._explain(86400)))


class TestSleepBlame(DiagnoseCase):
    def test_normal_system_holds_are_not_blamed(self):
        """powerd's assertion means the screen is on; cupsd's lets printers
        find the Mac. Blaming either is true and worthless."""
        self._system(120, cpu=10.0)
        self._hold("cupsd", "NetworkClientActive", 3600)
        self.assertNotIn("kept-awake", self._kinds(self._explain()))

    def test_a_real_holder_is_blamed_when_nobody_was_using_it(self):
        self._system(120, cpu=10.0)
        self._hold("Claude", "NoIdleSleepAssertion", 3600)
        found = [f for f in self._explain()["findings"] if f["kind"] == "kept-awake"]
        self.assertTrue(found)
        self.assertEqual(found[0]["severity"], "cause")
        self.assertIn("Claude", found[0]["headline"])

    def test_it_is_only_a_note_while_somebody_is_using_the_machine(self):
        """The display being on means awake is what was wanted.

        Without this check the feature cries wolf on every window in which
        somebody was simply working.
        """
        self._system(120, cpu=10.0)
        self._hold("Claude", "NoIdleSleepAssertion", 3600)
        self._hold("powerd", "PreventUserIdleSystemSleep", 3600)
        found = [f for f in self._explain()["findings"] if f["kind"] == "kept-awake"]
        self.assertTrue(found)
        self.assertEqual(found[0]["severity"], "note")

    def test_several_holders_are_one_finding_not_three(self):
        # Three near-identical entries push everything else off the screen.
        self._system(120, cpu=10.0)
        self._hold("Claude", "NoIdleSleepAssertion", 3600)
        self._hold("Docker", "PreventSystemSleep", 3500)
        self._hold("Slack", "PreventSystemSleep", 3400)
        found = [f for f in self._explain()["findings"] if f["kind"] == "kept-awake"]
        self.assertEqual(len(found), 1)
        self.assertEqual(len(found[0]["evidence"]["processes"]), 3)
        # And the sentence names them, not just the evidence: a headline that
        # blames one of three is the wrong answer to "what kept it awake".
        for process in ("Claude", "Docker", "Slack"):
            self.assertIn(process, found[0]["headline"])


class TestRanking(DiagnoseCase):
    def test_causes_come_before_costs(self):
        self._system(120, cpu=90.0)
        self._busy("Arc Helper", 200.0, 40, app="Arc")       # a cause
        self._busy("mds_stores", 80.0, 40, is_system=1)      # a cost
        severities = [f["severity"] for f in self._explain()["findings"]]
        self.assertEqual(severities, sorted(
            severities, key=lambda s: {"cause": 0, "cost": 1, "note": 2}[s]))

    def test_the_verdict_reads_as_a_sentence(self):
        self._system(120, cpu=70.0)
        self._busy("mds_stores", 90.0, 40, is_system=1)
        verdict = self._explain()["verdict"]
        self.assertTrue(verdict.endswith("."), verdict)
        # The precedence bug built this from the first character only.
        self.assertGreater(len(verdict), 20, verdict)


if __name__ == "__main__":
    unittest.main()
