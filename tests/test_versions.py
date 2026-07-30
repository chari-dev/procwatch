import os
import plistlib
import shutil
import tempfile
import unittest

from procwatch import config, db, versions


class VersionCase(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)
        versions.init(self.conn)

    def tearDown(self):
        self.conn.close()

    def _app(self, root, name, version, key="CFBundleShortVersionString"):
        folder = os.path.join(root, name + ".app", "Contents")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "Info.plist"), "wb") as handle:
            plistlib.dump({key: version, "CFBundleName": name}, handle)

    def _seen(self, app, version, first, last):
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO app_version (app, version, first_ts, last_ts) "
                "VALUES (?,?,?,?)", (app, version, first, last))

    def _samples(self, app, start, count, cpu, rss_mb=100, step=30):
        # Get-or-create: an identity is unique by (exe, args_sig), so a second
        # call for the same app is the same process, not a new one.
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO proc (exe, args_sig, cmdline_full, "
                "is_system, app) VALUES (?,'','',0,?)", (app + " Helper", app))
        pid = self.conn.execute(
            "SELECT id FROM proc WHERE exe = ? AND args_sig = ''",
            (app + " Helper",)).fetchone()[0]
        with self.conn:
            for i in range(count):
                self.conn.execute(
                    "INSERT OR REPLACE INTO sample_raw (ts, proc_id, cpu_avg, "
                    "cpu_max, cpu_max_ts, rss_avg, rss_max, nproc, samples) "
                    "VALUES (?,?,?,?,?,?,?,1,1)",
                    (start + i * step, pid, int(cpu * 10), int(cpu * 10),
                     start + i * step, int(rss_mb * 1024), int(rss_mb * 1024)))


class TestReadingVersions(VersionCase):
    def test_it_reads_the_version_from_the_bundle(self):
        folder = tempfile.mkdtemp()
        try:
            self._app(folder, "Arc", "1.157.1")
            self.assertEqual(versions.installed([folder]), {"Arc": "1.157.1"})
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_it_falls_back_to_the_build_version(self):
        # Some bundles carry only CFBundleVersion. No version at all is worse
        # than an ugly one.
        folder = tempfile.mkdtemp()
        try:
            self._app(folder, "Thing", "884", key="CFBundleVersion")
            self.assertEqual(versions.installed([folder]), {"Thing": "884"})
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_a_bundle_with_no_readable_plist_is_skipped(self):
        folder = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(folder, "Broken.app", "Contents"))
            self.assertEqual(versions.installed([folder]), {})
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_a_missing_folder_is_not_an_error(self):
        self.assertEqual(versions.installed(["/nope/nowhere"]), {})

    def test_seeing_the_same_version_again_extends_it(self):
        folder = tempfile.mkdtemp()
        try:
            self._app(folder, "Arc", "1.157.1")
            versions.tick(self.conn, now=1000, roots=[folder])
            versions.tick(self.conn, now=5000, roots=[folder])
            rows = self.conn.execute(
                "SELECT version, first_ts, last_ts FROM app_version").fetchall()
            self.assertEqual(rows, [("1.157.1", 1000, 5000)])
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_a_new_version_starts_a_new_row(self):
        folder = tempfile.mkdtemp()
        try:
            self._app(folder, "Arc", "1.157.1")
            versions.tick(self.conn, now=1000, roots=[folder])
            self._app(folder, "Arc", "1.158.0")
            versions.tick(self.conn, now=9000, roots=[folder])
            self.assertEqual(
                self.conn.execute("SELECT COUNT(*) FROM app_version").fetchone()[0],
                2)
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class TestUpdates(VersionCase):
    def test_a_first_sighting_is_not_an_update(self):
        """Otherwise every app is 'updated' on the day the tool is installed."""
        self._seen("Arc", "1.157.1", 1000, 2000)
        self.assertEqual(versions.updates(self.conn, since=0, now=3000), [])

    def test_a_version_change_is_an_update(self):
        self._seen("Arc", "1.157.1", 1000, 5000)
        self._seen("Arc", "1.158.0", 5000, 9000)
        found = versions.updates(self.conn, since=0, now=9000)
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["from_version"], found[0]["to_version"]),
                         ("1.157.1", "1.158.0"))


class TestRegressions(VersionCase):
    def _update(self, app, at, span=4000):
        self._seen(app, "old", at - span, at)
        self._seen(app, "new", at, at + span)

    def test_a_real_regression_is_reported_with_its_numbers(self):
        at = 1_000_000
        self._update("Arc", at)
        self._samples("Arc", at - 4000, 60, cpu=5.0)
        self._samples("Arc", at, 60, cpu=20.0)
        found = versions.regressions(self.conn, since=0, now=at + 4000)
        cpu = [f for f in found if f["metric"] == "cpu"]
        self.assertTrue(cpu)
        self.assertTrue(cpu[0]["worse"])
        self.assertEqual(cpu[0]["to_version"], "new")
        self.assertGreater(cpu[0]["ratio"], 3.0)

    def test_a_small_wobble_is_not_a_regression(self):
        """Software use is spiky. Crying regression at 20% would make this
        feature worse than not having it."""
        at = 1_000_000
        self._update("Arc", at)
        self._samples("Arc", at - 4000, 60, cpu=10.0)
        self._samples("Arc", at, 60, cpu=11.5)
        self.assertEqual(
            [f for f in versions.regressions(self.conn, since=0, now=at + 4000)
             if f["metric"] == "cpu"], [])

    def test_doubling_something_negligible_is_not_news(self):
        # 0.4% to 0.9% is a doubling and means nothing.
        at = 1_000_000
        self._update("Arc", at)
        self._samples("Arc", at - 4000, 60, cpu=0.4, rss_mb=10)
        self._samples("Arc", at, 60, cpu=0.9, rss_mb=10)
        self.assertEqual(versions.regressions(self.conn, since=0, now=at + 4000), [])

    def test_too_little_evidence_on_either_side_is_not_judged(self):
        at = 1_000_000
        self._update("Arc", at)
        self._samples("Arc", at - 4000, 60, cpu=5.0)
        self._samples("Arc", at, 3, cpu=40.0)      # three samples after
        self.assertEqual(versions.regressions(self.conn, since=0, now=at + 4000), [])

    def test_an_improvement_is_reported_too(self):
        # A feature that only finds bad news reads as one looking for it.
        at = 1_000_000
        self._update("Arc", at)
        self._samples("Arc", at - 4000, 60, cpu=30.0)
        self._samples("Arc", at, 60, cpu=6.0)
        found = [f for f in versions.regressions(self.conn, since=0, now=at + 4000)
                 if f["metric"] == "cpu"]
        self.assertTrue(found)
        self.assertFalse(found[0]["worse"])

    def test_memory_is_judged_as_well_as_cpu(self):
        at = 1_000_000
        self._update("Slack", at)
        self._samples("Slack", at - 4000, 60, cpu=1.0, rss_mb=400)
        self._samples("Slack", at, 60, cpu=1.0, rss_mb=1600)
        found = [f for f in versions.regressions(self.conn, since=0, now=at + 4000)
                 if f["metric"] == "memory_mb"]
        self.assertTrue(found)
        self.assertEqual(found[0]["unit"], "MB")

    def test_an_update_minutes_ago_is_not_judged_yet(self):
        at = 1_000_000
        self._seen("Arc", "old", at - 4000, at)
        self._seen("Arc", "new", at, at + 120)
        self._samples("Arc", at - 4000, 60, cpu=5.0)
        self._samples("Arc", at, 4, cpu=50.0)
        self.assertEqual(versions.regressions(self.conn, since=0, now=at + 120), [])


class TestCompared(unittest.TestCase):
    """Every update accounted for, including the ones nothing can be said about.

    The panel used to print only the updates whose usage changed enough to
    report, and a sentence when none did: "1 update, none of which measurably
    changed how much the application uses". That withheld which application and
    which versions, and ran together two different answers -- measured and
    unchanged, against not enough recorded yet to measure.
    """

    def _db(self):
        conn = db.connect(":memory:")
        db.init_schema(conn)
        versions.init(conn)
        return conn

    def _app(self, conn, name="Thing"):
        with conn:
            conn.execute("INSERT INTO proc (id, exe, args_sig, cmdline_full, "
                         "app, is_system) VALUES (1,?,'','',?,0)", (name, name))

    def _samples(self, conn, start, count, cpu, rss_mb):
        with conn:
            for i in range(count):
                ts = start + i * 30
                conn.execute(
                    "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, "
                    "cpu_max_ts, rss_avg, rss_max, nproc, samples) "
                    "VALUES (?,1,?,?,?,?,?,1,1)",
                    (ts, int(cpu * 10), int(cpu * 10), ts,
                     int(rss_mb * 1024), int(rss_mb * 1024)))

    def _version(self, conn, app, version, first, last):
        with conn:
            conn.execute("INSERT INTO app_version (app, version, first_ts, "
                         "last_ts) VALUES (?,?,?,?)", (app, version, first, last))

    def test_an_update_with_nothing_recorded_before_it_says_so(self):
        conn = self._db()
        self._app(conn)
        base = 1750000000
        self._version(conn, "Thing", "1.0", base, base + 3600)
        self._version(conn, "Thing", "2.0", base + 3600, base + 7200)
        self._samples(conn, base + 3600, 60, 5.0, 100)   # only after
        rows = versions.compared(conn, since=base - 86400, now=base + 7200)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "unrecorded")
        self.assertIn("before the update", rows[0]["why"])
        self.assertEqual(rows[0]["app"], "Thing")
        self.assertEqual(rows[0]["from_version"], "1.0")

    def test_an_update_nobody_has_opened_since_says_that_instead(self):
        conn = self._db()
        self._app(conn)
        base = 1750000000
        self._version(conn, "Thing", "1.0", base, base + 3600)
        self._version(conn, "Thing", "2.0", base + 3600, base + 7200)
        self._samples(conn, base, 60, 5.0, 100)          # only before
        rows = versions.compared(conn, since=base - 86400, now=base + 7200)
        self.assertEqual(rows[0]["state"], "unrecorded")
        self.assertIn("not run since", rows[0]["why"])

    def test_an_update_minutes_old_is_too_soon_not_unrecorded(self):
        """The two read completely differently and only one is true.

        A window a minute wide cannot hold forty samples and holds none at all
        in the first thirty seconds. Checking "are there samples" before "is the
        window long enough" reported an application that had been running the
        whole time as one that was not running before its own update -- which is
        what Procwatch said about itself thirty seconds after installing 1.4.0.
        """
        conn = self._db()
        self._app(conn)
        base = 1750000000
        self._version(conn, "Thing", "1.0", base, base + 3600)
        self._version(conn, "Thing", "2.0", base + 3600, base + 3630)
        self._samples(conn, base, 100, 5.0, 100)      # running all along
        rows = versions.compared(conn, since=base - 86400, now=base + 3630)
        self.assertEqual(rows[0]["state"], "too-soon")
        self.assertNotIn("not running", rows[0]["why"])

    def test_a_fresh_update_reports_how_far_off_a_verdict_is(self):
        # "Still measuring, 12 of 40 samples" is a different answer from "no
        # change", and the reader can act on the difference: wait.
        conn = self._db()
        self._app(conn)
        base = 1750000000
        self._version(conn, "Thing", "1.0", base, base + 600)
        self._version(conn, "Thing", "2.0", base + 600, base + 900)
        self._samples(conn, base, 20, 5.0, 100)
        self._samples(conn, base + 600, 10, 5.0, 100)
        rows = versions.compared(conn, since=base - 86400, now=base + 900)
        self.assertEqual(rows[0]["state"], "too-soon")
        self.assertEqual(rows[0]["samples_needed"], versions.MIN_SAMPLES)
        self.assertLess(rows[0]["samples"], versions.MIN_SAMPLES)

    def test_measured_and_unchanged_carries_the_measurement(self):
        conn = self._db()
        self._app(conn)
        base = 1750000000
        self._version(conn, "Thing", "1.0", base, base + 3600)
        self._version(conn, "Thing", "2.0", base + 3600, base + 7200)
        self._samples(conn, base, 60, 10.0, 300)
        self._samples(conn, base + 3600, 60, 10.4, 305)
        rows = versions.compared(conn, since=base - 86400, now=base + 7200)
        self.assertEqual(rows[0]["state"], "same")
        self.assertIsNone(rows[0]["change"])
        # Not a shrug: the numbers it was decided from come with it.
        self.assertEqual(len(rows[0]["cpu"]), 2)
        self.assertEqual(len(rows[0]["memory_mb"]), 2)

    def test_a_real_change_is_reported_with_its_direction(self):
        conn = self._db()
        self._app(conn)
        base = 1750000000
        self._version(conn, "Thing", "1.0", base, base + 3600)
        self._version(conn, "Thing", "2.0", base + 3600, base + 7200)
        self._samples(conn, base, 60, 10.0, 300)
        self._samples(conn, base + 3600, 60, 40.0, 300)
        rows = versions.compared(conn, since=base - 86400, now=base + 7200)
        self.assertEqual(rows[0]["state"], "worse")
        self.assertTrue(rows[0]["change"]["worse"])
        self.assertEqual(rows[0]["change"]["metric"], "cpu")

    def test_the_biggest_change_wins_not_the_first_checked(self):
        # An update that halves the CPU and triples the memory has one headline,
        # and it is not whichever metric happens to be checked first.
        conn = self._db()
        self._app(conn)
        base = 1750000000
        self._version(conn, "Thing", "1.0", base, base + 3600)
        self._version(conn, "Thing", "2.0", base + 3600, base + 7200)
        self._samples(conn, base, 60, 20.0, 300)
        self._samples(conn, base + 3600, 60, 10.0, 1500)
        rows = versions.compared(conn, since=base - 86400, now=base + 7200)
        self.assertEqual(rows[0]["change"]["metric"], "memory_mb")

    def test_regressions_still_reports_only_the_measured_changes(self):
        conn = self._db()
        self._app(conn)
        base = 1750000000
        self._version(conn, "Thing", "1.0", base, base + 3600)
        self._version(conn, "Thing", "2.0", base + 3600, base + 7200)
        self._samples(conn, base, 60, 10.0, 300)
        self._samples(conn, base + 3600, 60, 10.4, 305)
        self.assertEqual(versions.regressions(conn, since=base - 86400,
                                              now=base + 7200), [])
        self.assertEqual(len(versions.compared(conn, since=base - 86400,
                                               now=base + 7200)), 1)


class TestHistory(unittest.TestCase):
    """Every version, so any two can be compared.

    The step-by-step list answers "did this update change anything". It cannot
    answer "is this heavier than it was three versions ago", because each
    comparison only looks at its own step -- and four steps of "no real change"
    can still have doubled the memory between them.
    """

    def _db(self):
        conn = db.connect(":memory:")
        db.init_schema(conn)
        versions.init(conn)
        with conn:
            conn.execute("INSERT INTO proc (id, exe, args_sig, cmdline_full, "
                         "app, is_system) VALUES (1,'Thing','','','Thing',0)")
        return conn

    def _samples(self, conn, start, count, cpu, rss_mb):
        with conn:
            for i in range(count):
                ts = start + i * 30
                conn.execute(
                    "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, "
                    "cpu_max_ts, rss_avg, rss_max, nproc, samples) "
                    "VALUES (?,1,?,?,?,?,?,1,1)",
                    (ts, int(cpu * 10), int(cpu * 10), ts,
                     int(rss_mb * 1024), int(rss_mb * 1024)))

    def _creep(self, conn):
        """Four updates that each changed nothing, and doubled the memory."""
        base = 1750000000
        step = 4000
        for i, (version, mem) in enumerate((("1.0", 300), ("1.1", 380),
                                            ("1.2", 460), ("1.3", 540),
                                            ("1.4", 620))):
            start = base + i * step
            with conn:
                conn.execute("INSERT INTO app_version (app, version, first_ts, "
                             "last_ts) VALUES ('Thing',?,?,?)",
                             (version, start, start + step))
            self._samples(conn, start, 100, 10.0, mem)
        # `now` is deliberately later than the newest version's last sighting.
        # With the two equal, a window ending at last_ts and one ending at now
        # are the same window, and the difference between them cannot be tested.
        return base, base + 5 * step + 3000

    def test_every_version_is_measured_over_the_span_it_was_current(self):
        conn = self._db()
        base, now = self._creep(conn)
        entry = versions.history(conn, now=now)[0]
        self.assertEqual([v["version"] for v in entry["versions"]],
                         ["1.0", "1.1", "1.2", "1.3", "1.4"])
        self.assertEqual(entry["versions"][0]["memory_mb"], 300.0)
        self.assertEqual(entry["versions"][-1]["memory_mb"], 620.0)

    def test_a_creep_no_single_step_would_report_is_reported(self):
        # Each step is 80 MB on 300, which is nowhere near half again. End to
        # end it is more than double, and that is the finding.
        conn = self._db()
        base, now = self._creep(conn)
        entry = versions.history(conn, now=now)[0]
        self.assertIsNotNone(entry["drift"])
        self.assertIn("memory_mb", entry["drift"])
        self.assertTrue(entry["drift"]["memory_mb"]["worse"])
        self.assertEqual(entry["drift"]["from_version"], "1.0")
        self.assertEqual(entry["drift"]["to_version"], "1.4")
        # And no individual step crosses the threshold, which is the point.
        self.assertEqual(versions.regressions(conn, since=base - 1, now=now), [])

    def test_a_step_is_judged_against_the_next_version_only(self):
        """The window after an update ends when the version after it arrives.

        Running it to `now` meant an old update was compared against every
        version since. The creeping fixture above had its first step reported as
        300 to 500 MB -- an average of the four versions that followed it -- and
        called a regression that no single step actually was.
        """
        conn = self._db()
        base, now = self._creep(conn)
        found = versions.updates(conn, since=base - 1, now=now)
        by_step = {u["to_version"]: u for u in found}
        starts = {v["version"]: v["first_ts"]
                  for v in versions.history(conn, now=now)[0]["versions"]}
        # 1.1's window stops when 1.2 appears, and so on down the line.
        self.assertEqual(by_step["1.1"]["after_end"], starts["1.2"])
        self.assertEqual(by_step["1.2"]["after_end"], starts["1.3"])
        self.assertEqual(by_step["1.3"]["after_end"], starts["1.4"])
        # The newest has nothing after it, so it runs to now -- not to whenever
        # the application was last seen. last_ts stops advancing the moment it
        # is quit, and judging the current version over a window that ends there
        # measures it only while it happened to be open.
        self.assertEqual(by_step["1.4"]["after_end"], now)
        newest = [v for v in versions.history(conn, now=now)[0]["versions"]
                  if v["version"] == "1.4"][0]
        self.assertGreater(now, newest["first_ts"] + 3000)
        self.assertEqual(by_step["1.4"]["after_end"], now)
        # And `now` is genuinely later than the last sample, so a window that
        # stopped at last_ts would be visibly shorter.
        last_sample = conn.execute(
            "SELECT MAX(ts) FROM sample_raw").fetchone()[0]
        self.assertGreater(now, last_sample)

    def test_an_application_that_never_changed_says_so(self):
        conn = self._db()
        base = 1750000000
        for i, version in enumerate(("1.0", "1.1")):
            start = base + i * 4000
            with conn:
                conn.execute("INSERT INTO app_version (app, version, first_ts, "
                             "last_ts) VALUES ('Thing',?,?,?)",
                             (version, start, start + 4000))
            self._samples(conn, start, 100, 10.0, 300)
        entry = versions.history(conn, now=base + 8000)[0]
        self.assertTrue(entry["drift"]["same"])

    def test_a_version_with_too_little_recorded_is_marked_not_compared(self):
        conn = self._db()
        base = 1750000000
        with conn:
            conn.execute("INSERT INTO app_version (app, version, first_ts, "
                         "last_ts) VALUES ('Thing','1.0',?,?)",
                         (base, base + 4000))
            conn.execute("INSERT INTO app_version (app, version, first_ts, "
                         "last_ts) VALUES ('Thing','1.1',?,?)",
                         (base + 4000, base + 8000))
        self._samples(conn, base, 100, 10.0, 300)
        self._samples(conn, base + 4000, 5, 10.0, 300)     # barely seen
        entry = versions.history(conn, now=base + 8000)[0]
        self.assertTrue(entry["versions"][0]["enough"])
        self.assertFalse(entry["versions"][1]["enough"])
        # Two versions, only one measurable, so there is nothing to compare.
        self.assertIsNone(entry["drift"])

    def test_a_version_never_seen_running_carries_no_numbers(self):
        conn = self._db()
        base = 1750000000
        with conn:
            conn.execute("INSERT INTO app_version (app, version, first_ts, "
                         "last_ts) VALUES ('Thing','1.0',?,?)",
                         (base, base + 4000))
        entry = versions.history(conn, now=base + 4000)[0]
        self.assertIsNone(entry["versions"][0]["cpu"])
        self.assertEqual(entry["versions"][0]["samples"], 0)

    def test_the_newest_version_is_measured_up_to_now(self):
        # Not to last_ts, which for the current version is whenever the app was
        # last seen and would keep shrinking the window it is judged over.
        conn = self._db()
        base = 1750000000
        with conn:
            conn.execute("INSERT INTO app_version (app, version, first_ts, "
                         "last_ts) VALUES ('Thing','1.0',?,?)",
                         (base, base + 100))
        self._samples(conn, base, 100, 10.0, 300)
        entry = versions.history(conn, now=base + 9000)[0]
        self.assertEqual(entry["versions"][0]["until_ts"], base + 9000)
        self.assertEqual(entry["versions"][0]["samples"], 100)


class TestOwnVersion(unittest.TestCase):
    """Procwatch has to be able to see its own updates.

    It could not. menubar/build.sh declared CFBundleShortVersionString as 1.0 in
    a heredoc, and had done through four releases -- so the string never moved,
    and by this module's own correct rule an application whose declared version
    has not changed has not updated. The tool that reports on what applications
    do after an update was blind to exactly one application: itself.
    """

    def _plist(self):
        import os
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(here, "menubar", "build.sh")

    def test_the_bundle_does_not_hardcode_a_version(self):
        with open(self._plist()) as handle:
            script = handle.read()
        self.assertNotIn("<string>1.0</string>", script)
        self.assertIn("__VERSION__", script)
        self.assertIn("config.VERSION", script)

    def test_the_version_has_one_home(self):
        from procwatch import config
        self.assertTrue(config.VERSION)
        # Three parts, so it sorts and compares like a version rather than like
        # the "1.0" that never moved.
        self.assertEqual(len(config.VERSION.split(".")), 3)

    def test_the_bundle_also_carries_a_build_identifier(self):
        # Two applications can ship the same marketing version; the build is
        # what tells those apart, and 29 of the 38 applications on the machine
        # this was written on set it differently from the short string.
        with open(self._plist()) as handle:
            script = handle.read()
        self.assertIn("CFBundleVersion", script)
        self.assertIn("__BUILD__", script)

    def test_the_plist_heredoc_stays_quoted(self):
        # Unquoted, every $ in the plist's prose would expand -- and one of the
        # values is a usage description written for a person to read.
        with open(self._plist()) as handle:
            script = handle.read()
        self.assertIn("<<'PLIST'", script)


if __name__ == "__main__":
    unittest.main()
