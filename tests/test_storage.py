import os
import shutil
import tempfile
import unittest

from procwatch import db, storage


class TestScan(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)
        self.dir = tempfile.mkdtemp()
        self.apps = os.path.join(self.dir, "Applications")
        self.support = os.path.join(self.dir, "Support")
        os.makedirs(os.path.join(self.apps, "Notes.app", "Contents"))
        os.makedirs(os.path.join(self.support, "Notes"))
        self._write(os.path.join(self.apps, "Notes.app", "Contents", "bin"), 4096)
        self._write(os.path.join(self.support, "Notes", "db.sqlite"), 8192)
        self.roots = [("bundle", self.apps), ("support", self.support)]

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        self.conn.close()

    def _write(self, path, size):
        with open(path, "wb") as handle:
            handle.write(b"\0" * size)

    def test_it_totals_a_bundle_and_its_data_together(self):
        storage.scan(self.conn, now=100, roots=self.roots)
        got = storage.usage(self.conn)[0]
        self.assertEqual(got["app"], "Notes")
        self.assertGreaterEqual(got["total"], 4096 + 8192)
        self.assertGreater(got["bundle"], 0)
        self.assertGreater(got["support"], 0)

    def test_the_dot_app_suffix_is_not_part_of_the_name(self):
        # It has to match the name the charts use, or the two views of the
        # same application never line up.
        storage.scan(self.conn, now=100, roots=self.roots)
        self.assertEqual(storage.usage(self.conn)[0]["app"], "Notes")

    def test_a_symlink_is_not_followed(self):
        """/Applications is full of links into the same bundle.

        Following them counts the target repeatedly, and a link that points at
        an ancestor never terminates at all.
        """
        target = os.path.join(self.apps, "Notes.app")
        os.symlink(target, os.path.join(self.apps, "Loop.app"))
        storage.scan(self.conn, now=100, roots=self.roots)
        names = [a["app"] for a in storage.usage(self.conn)]
        self.assertNotIn("Loop", names)

    def test_a_directory_loop_terminates(self):
        deep = os.path.join(self.apps, "Notes.app", "Contents", "self")
        os.symlink(os.path.join(self.apps, "Notes.app"), deep)
        storage.scan(self.conn, now=100, roots=self.roots)   # must return
        self.assertTrue(storage.usage(self.conn))

    def test_hidden_entries_are_skipped(self):
        os.makedirs(os.path.join(self.apps, ".Trash"))
        self._write(os.path.join(self.apps, ".Trash", "junk"), 1024)
        storage.scan(self.conn, now=100, roots=self.roots)
        self.assertNotIn(".Trash", [a["app"] for a in storage.usage(self.conn)])

    def test_a_missing_root_is_not_an_error(self):
        storage.scan(self.conn, now=100,
                     roots=[("bundle", os.path.join(self.dir, "nope"))])

    def test_rescanning_replaces_rather_than_accumulates(self):
        storage.scan(self.conn, now=100, roots=self.roots)
        first = storage.usage(self.conn)[0]["total"]
        storage.scan(self.conn, now=200, roots=self.roots)
        self.assertEqual(storage.usage(self.conn)[0]["total"], first)

    def test_a_scan_is_not_due_again_the_same_day(self):
        # The walk is expensive and the number moves once a week.
        self.assertTrue(storage.due(self.conn, now=100))
        storage.scan(self.conn, now=100, roots=self.roots)
        self.assertFalse(storage.due(self.conn, now=100 + 3600))
        self.assertTrue(storage.due(self.conn, now=100 + storage.DAY))


if __name__ == "__main__":
    unittest.main()


class TestGrowth(unittest.TestCase):
    """What grew, which is the question people are actually asking.

    Current sizes answer "what is big", and a folder that has been 40 GB for
    two years is not why the disk filled up this week.
    """

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)
        storage.init(self.conn)

    def tearDown(self):
        self.conn.close()

    def _snapshot(self, day, sizes):
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO storage_history (day, app, kind, bytes) "
                "VALUES (?,?,'support',?)",
                [(day * storage.DAY, app, size) for app, size in sizes.items()])

    def test_no_snapshots_at_all_is_answered_not_raised(self):
        """A fresh install has none, and the dashboard asks anyway.

        Comparing the newest snapshot with the oldest reads both ends of a
        list, so an empty list is the case that breaks rather than the case
        that returns nothing.
        """
        report = storage.growth(self.conn, since=0, now=200 * storage.DAY)
        self.assertEqual(report["apps"], [])
        self.assertEqual(report["days_compared"], 0)
        self.assertIsNone(report["from_day"])

    def test_one_snapshot_cannot_report_growth(self):
        # And must say so rather than reporting everything as newly appeared.
        self._snapshot(100, {"Claude": 1000})
        report = storage.growth(self.conn, since=0, now=200 * storage.DAY)
        self.assertEqual(report["apps"], [])
        self.assertEqual(report["days_compared"], 1)

    def test_it_reports_the_difference_between_two_scans(self):
        self._snapshot(100, {"Claude": 1_000_000, "Xcode": 5_000_000})
        self._snapshot(107, {"Claude": 12_000_000, "Xcode": 5_000_000})
        report = storage.growth(self.conn, since=0, now=200 * storage.DAY)
        self.assertEqual(report["apps"][0]["app"], "Claude")
        self.assertEqual(report["apps"][0]["change"], 11_000_000)
        # Xcode did not move, so it is not news.
        self.assertEqual(len(report["apps"]), 1)

    def test_the_biggest_mover_comes_first_in_either_direction(self):
        # A 9 GB deletion is more interesting than a 1 GB gain.
        self._snapshot(100, {"Old": 9_000_000, "New": 0})
        self._snapshot(107, {"Old": 0, "New": 1_000_000})
        apps = storage.growth(self.conn, since=0, now=200 * storage.DAY)["apps"]
        self.assertEqual(apps[0]["app"], "Old")
        self.assertLess(apps[0]["change"], 0)

    def test_appearing_and_disappearing_are_said_differently(self):
        # "+4 GB" and "installed, 4 GB" are different news.
        self._snapshot(100, {"Gone": 4_000_000})
        self._snapshot(107, {"Fresh": 4_000_000})
        states = {a["app"]: a["state"] for a in
                  storage.growth(self.conn, since=0, now=200 * storage.DAY)["apps"]}
        self.assertEqual(states["Gone"], "removed")
        self.assertEqual(states["Fresh"], "installed")

    def test_the_total_is_the_net_change(self):
        self._snapshot(100, {"A": 1_000, "B": 5_000})
        self._snapshot(107, {"A": 3_000, "B": 4_000})
        report = storage.growth(self.conn, since=0, now=200 * storage.DAY)
        self.assertEqual(report["total_change"], (3_000 - 1_000) + (4_000 - 5_000))

    def test_two_scans_on_one_day_correct_that_day(self):
        """A machine restarted four times must not look like four days."""
        now = 100 * storage.DAY + 3600
        folder = tempfile.mkdtemp()
        try:
            apps = os.path.join(folder, "Applications")
            os.makedirs(os.path.join(apps, "Notes.app"))
            with open(os.path.join(apps, "Notes.app", "bin"), "wb") as h:
                h.write(b"\0" * 4096)
            roots = [("bundle", apps)]
            storage.scan(self.conn, now=now, roots=roots)
            storage.scan(self.conn, now=now + 7200, roots=roots)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        days = self.conn.execute(
            "SELECT COUNT(DISTINCT day) FROM storage_history").fetchone()[0]
        self.assertEqual(days, 1)

    def test_a_scan_writes_both_the_current_view_and_the_history(self):
        folder = tempfile.mkdtemp()
        try:
            apps = os.path.join(folder, "Applications")
            os.makedirs(os.path.join(apps, "Notes.app"))
            with open(os.path.join(apps, "Notes.app", "bin"), "wb") as h:
                h.write(b"\0" * 8192)
            storage.scan(self.conn, now=100 * storage.DAY, roots=[("bundle", apps)])
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        self.assertTrue(storage.usage(self.conn))
        self.assertTrue(self.conn.execute(
            "SELECT COUNT(*) FROM storage_history").fetchone()[0])

    def test_old_snapshots_are_forgotten(self):
        self._snapshot(1, {"Ancient": 1})
        self._snapshot(500, {"Recent": 1})
        storage.prune(self.conn, now=500 * storage.DAY, keep_days=100)
        left = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT app FROM storage_history").fetchall()]
        self.assertEqual(left, ["Recent"])
