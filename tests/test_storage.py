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
