import unittest
from procwatch import config, db, rollup

DAY = 86400


class TestRetention(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)
        self.pid = self.conn.execute(
            "INSERT INTO proc (exe, args_sig, cmdline_full) VALUES ('x','','x')"
        ).lastrowid

    def _fill(self, days, step):
        rows = [(ts, self.pid, 100, 100, ts, 50, 50, 1, 1)
                for ts in range(0, days * DAY, step)]
        self.conn.executemany(
            "INSERT OR REPLACE INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, "
            "cpu_max_ts, rss_avg, rss_max, nproc, samples) VALUES (?,?,?,?,?,?,?,?,?)",
            rows)
        self.conn.commit()

    def _counts(self):
        return {t.name: self.conn.execute(
            "SELECT COUNT(*) FROM sample_%s" % t.name).fetchone()[0]
            for t in config.TIERS}

    def _total_rows(self):
        return sum(self._counts().values())

    def _total_samples(self):
        return sum(
            self.conn.execute(
                "SELECT COALESCE(SUM(samples), 0) FROM sample_%s" % t.name).fetchone()[0]
            for t in config.TIERS)

    def test_two_simulated_years_stay_bounded(self):
        self._fill(days=730, step=3600)     # hourly rows across two years
        now = 730 * DAY
        for _ in range(200):                # run rollup to convergence
            rollup.run(self.conn, now)
        counts = self._counts()
        # raw only ever holds rows still inside its own 7-day window (the
        # most recent 168 hourly rows of the two-year fill). That window is
        # measured against `now`, which is pinned here rather than advancing,
        # so raw converges to its window size, not to zero -- confirmed by
        # running 1000 rollup.run() calls and observing the count is stable
        # at 168, not still draining.
        self.assertLessEqual(counts["raw"], config.TIERS[0].retain_seconds // 3600 + 1)
        # 12000 is tight enough that it would fail if retention/rollup did
        # nothing at all: the fixture inserts 17520 raw rows, comfortably
        # above this bound, while the converged total observed here is
        # 10220 -- so this is a real assertion about collapsing/pruning
        # actually happening, not a bound the fixture can never reach.
        self.assertLess(sum(counts.values()), 12000)

    def test_each_tier_holds_only_its_own_window(self):
        self._fill(days=730, step=3600)
        now = 730 * DAY
        for _ in range(200):
            rollup.run(self.conn, now)
        for tier in config.TIERS:
            if tier.retain_seconds is None:
                continue
            oldest = self.conn.execute(
                "SELECT MIN(ts) FROM sample_%s" % tier.name).fetchone()[0]
            # Given this fixture (two years of hourly data), every finite
            # tier should hold something -- a tier that came up empty is a
            # failure, not a case to silently skip past.
            self.assertIsNotNone(oldest, "%s tier is unexpectedly empty" % tier.name)
            self.assertGreaterEqual(oldest, now - tier.retain_seconds)

    def test_the_archive_tier_still_holds_the_oldest_data(self):
        self._fill(days=730, step=3600)
        now = 730 * DAY
        for _ in range(200):
            rollup.run(self.conn, now)
        oldest = self.conn.execute("SELECT MIN(ts) FROM sample_archive").fetchone()[0]
        self.assertIsNotNone(oldest)
        self.assertLess(oldest, 30 * DAY)

    def test_low_disk_triggers_an_early_prune(self):
        # disk_guard coarsens early (collapses into the next tier) rather
        # than deleting -- a disk_guard that returns True but does nothing,
        # or one that reverts to deleting outright, must both fail this
        # test: rows must actually leave raw, none may remain below the
        # halved cutoff, and every sample must survive the move.
        self._fill(days=10, step=3600)
        now = 10 * DAY
        before_raw = self.conn.execute("SELECT COUNT(*) FROM sample_raw").fetchone()[0]
        before_samples = self._total_samples()
        ran = rollup.disk_guard(self.conn, now=now,
                                free_bytes=config.MIN_FREE_BYTES - 1)
        self.assertTrue(ran)
        after_raw = self.conn.execute("SELECT COUNT(*) FROM sample_raw").fetchone()[0]
        self.assertLess(after_raw, before_raw)
        cutoff = now - config.TIERS[0].retain_seconds // 2
        remaining_below_cutoff = self.conn.execute(
            "SELECT COUNT(*) FROM sample_raw WHERE ts < ?", (cutoff,)).fetchone()[0]
        self.assertEqual(remaining_below_cutoff, 0)
        self.assertEqual(self._total_samples(), before_samples)

    def test_ample_disk_does_not_prune_early(self):
        self._fill(days=10, step=3600)
        before = self._counts()
        ran = rollup.disk_guard(self.conn, now=10 * DAY,
                                free_bytes=config.MIN_FREE_BYTES * 10)
        self.assertFalse(ran)
        self.assertEqual(self._counts(), before)

    def test_disk_guard_does_not_destroy_unrolled_history(self):
        # The guard used to DELETE with a halved cutoff, bypassing the
        # collapse-before-prune protections and erasing 16720 rows that had
        # never reached any coarser tier.
        n = 10 * DAY // config.INTERVAL
        self.conn.executemany(
            "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, cpu_max_ts, "
            "rss_avg, rss_max, nproc, samples) VALUES (?,?,10,10,?,50,50,1,1)",
            [(i * config.INTERVAL, self.pid, i * config.INTERVAL) for i in range(n)])
        self.conn.commit()
        now = n * config.INTERVAL
        rollup.run(self.conn, now)
        before = self._total_samples()
        rollup.disk_guard(self.conn, now, config.MIN_FREE_BYTES - 1)
        self.assertEqual(self._total_samples(), before)

    def test_disk_guard_actually_reduces_row_count(self):
        # It must still do its job: fewer rows than leaving it alone.
        n = 10 * DAY // config.INTERVAL
        self.conn.executemany(
            "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, cpu_max_ts, "
            "rss_avg, rss_max, nproc, samples) VALUES (?,?,10,10,?,50,50,1,1)",
            [(i * config.INTERVAL, self.pid, i * config.INTERVAL) for i in range(n)])
        self.conn.commit()
        untouched = self.conn.execute("SELECT COUNT(*) FROM sample_raw").fetchone()[0]
        rollup.disk_guard(self.conn, n * config.INTERVAL, config.MIN_FREE_BYTES - 1)
        self.assertLess(self._total_rows(), untouched)


if __name__ == "__main__":
    unittest.main()
