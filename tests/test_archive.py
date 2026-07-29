import os
import shutil
import sqlite3
import tempfile
import unittest

from procwatch import archive, db


def _make_db(path, samples=3, ts0=1750000000):
    conn = db.connect(path)
    db.init_schema(conn)
    with conn:
        conn.execute("INSERT INTO proc (id, exe, args_sig, cmdline_full) "
                     "VALUES (1, 'thing', '', '')")
        for i in range(samples):
            conn.execute(
                "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, "
                "cpu_max_ts, rss_avg, rss_max, nproc, samples) "
                "VALUES (?,1,10,20,?,100,200,1,1)", (ts0 + i * 30, ts0 + i * 30))
    conn.close()
    return path


class TestBackup(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.source = _make_db(os.path.join(self.dir, "live.db"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _count(self, path):
        conn = sqlite3.connect(path)
        try:
            return conn.execute("SELECT COUNT(*) FROM sample_raw").fetchone()[0]
        finally:
            conn.close()

    def test_it_copies_every_row(self):
        out = archive.backup(os.path.join(self.dir, "copy.db"), self.source)
        self.assertEqual(self._count(out), 3)

    def test_a_directory_gets_a_dated_file_inside_it(self):
        target = os.path.join(self.dir, "backups")
        os.makedirs(target)
        out = archive.backup(target, self.source)
        self.assertEqual(os.path.dirname(out), target)
        self.assertTrue(os.path.basename(out).startswith("procwatch-"))

    def test_it_captures_writes_made_while_the_sampler_holds_the_file_open(self):
        # The whole reason for using the backup API rather than copying: the
        # recorder writes every thirty seconds and never closes the database.
        live = db.connect(self.source)
        with live:
            live.execute(
                "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, "
                "cpu_max_ts, rss_avg, rss_max, nproc, samples) "
                "VALUES (1750009999,1,5,5,1750009999,1,1,1,1)")
        try:
            out = archive.backup(os.path.join(self.dir, "hot.db"), self.source)
            self.assertEqual(self._count(out), 4)
        finally:
            live.close()

    def test_the_copy_is_a_single_file(self):
        # A backup that is only valid alongside two sidecar files is a trap
        # for anyone who moves it with the Finder. The copy inherits WAL from
        # the live database unless it is told otherwise, and the sidecars only
        # appear once something writes -- so the mode is asserted rather than
        # the absence of files, which would pass either way.
        out = archive.backup(os.path.join(self.dir, "one.db"), self.source)
        conn = sqlite3.connect(out)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(mode, "delete")
        self.assertFalse(os.path.exists(out + "-wal"))

    def test_backing_up_a_database_that_is_not_there_says_so(self):
        with self.assertRaises(RuntimeError):
            archive.backup(os.path.join(self.dir, "out.db"),
                           os.path.join(self.dir, "absent.db"))


class TestRestore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.live = _make_db(os.path.join(self.dir, "live.db"), samples=2)
        self.other = _make_db(os.path.join(self.dir, "other.db"),
                              samples=7, ts0=1700000000)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _count(self, path):
        conn = sqlite3.connect(path)
        try:
            return conn.execute("SELECT COUNT(*) FROM sample_raw").fetchone()[0]
        finally:
            conn.close()

    def test_it_replaces_the_contents(self):
        archive.restore(self.other, self.live)
        self.assertEqual(self._count(self.live), 7)

    def test_it_keeps_a_copy_of_what_it_replaced(self):
        _, previous = archive.restore(self.other, self.live)
        self.assertIsNotNone(previous)
        self.assertEqual(self._count(previous), 2)

    def test_it_refuses_a_file_that_is_not_a_database(self):
        junk = os.path.join(self.dir, "notes.txt")
        with open(junk, "w") as handle:
            handle.write("this is not a database\n")
        with self.assertRaises(RuntimeError):
            archive.restore(junk, self.live)
        # and the real history is untouched
        self.assertEqual(self._count(self.live), 2)

    def test_it_refuses_a_database_that_is_not_ours(self):
        stranger = os.path.join(self.dir, "stranger.db")
        conn = sqlite3.connect(stranger)
        conn.execute("CREATE TABLE notes (id INTEGER)")
        conn.commit()
        conn.close()
        with self.assertRaises(RuntimeError):
            archive.restore(stranger, self.live)
        self.assertEqual(self._count(self.live), 2)

    def test_it_refuses_to_restore_a_file_over_itself(self):
        with self.assertRaises(RuntimeError):
            archive.restore(self.live, self.live)
        self.assertEqual(self._count(self.live), 2)

    def test_the_restored_database_is_usable_by_the_sampler(self):
        # A restored file may predate columns the current code writes.
        archive.restore(self.other, self.live)
        conn = db.connect(self.live)
        try:
            conn.execute("SELECT energy, stuck FROM sample_raw LIMIT 1")
        finally:
            conn.close()

    def test_an_older_schema_gains_the_columns_it_lacks(self):
        old = os.path.join(self.dir, "old.db")
        conn = sqlite3.connect(old)
        conn.executescript("""
            CREATE TABLE proc (id INTEGER PRIMARY KEY, exe TEXT NOT NULL,
                               args_sig TEXT NOT NULL, cmdline_full TEXT NOT NULL);
            CREATE TABLE sample_raw (ts INTEGER, proc_id INTEGER, cpu_avg INTEGER,
                               cpu_max INTEGER, cpu_max_ts INTEGER, rss_avg INTEGER,
                               rss_max INTEGER, nproc INTEGER, samples INTEGER,
                               PRIMARY KEY (ts, proc_id));
            CREATE TABLE system_raw (ts INTEGER PRIMARY KEY);
            CREATE TABLE sampler_state (proc_id INTEGER PRIMARY KEY);
        """)
        conn.commit()
        conn.close()
        archive.restore(old, self.live)
        conn = sqlite3.connect(self.live)
        try:
            names = {r[1] for r in conn.execute("PRAGMA table_info(sample_raw)")}
        finally:
            conn.close()
        self.assertIn("energy", names)

    def test_describe_reports_the_span_it_would_restore(self):
        text = archive.describe(self.other)
        self.assertIn("7 samples", text)

    def test_describe_explains_a_file_it_would_refuse(self):
        junk = os.path.join(self.dir, "junk.bin")
        with open(junk, "wb") as handle:
            handle.write(b"\x00\x01\x02")
        self.assertTrue(archive.describe(junk))
        self.assertNotIn("samples", archive.describe(junk))


if __name__ == "__main__":
    unittest.main()
