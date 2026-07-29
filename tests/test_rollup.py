import unittest
from procwatch import config, db, rollup

HOUR = 3600
DAY = 86400


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)
        self.pid = self.conn.execute(
            "INSERT INTO proc (exe, args_sig, cmdline_full) VALUES ('x','','x')"
        ).lastrowid

    def raw(self, ts, cpu, samples=1, cpu_max=None, cpu_max_ts=None,
            rss=100, rss_max=None, nproc=1):
        self.conn.execute(
            "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, cpu_max_ts, "
            "rss_avg, rss_max, nproc, samples) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, self.pid, cpu, cpu_max if cpu_max is not None else cpu,
             cpu_max_ts if cpu_max_ts is not None else ts,
             rss, rss_max if rss_max is not None else rss, nproc, samples))
        self.conn.commit()

    def system(self, ts, samples=1, disk_free_kb=1000, **kwargs):
        cols = dict(cpu_busy=0, load1=0, mem_used_kb=0, mem_comp_kb=0, swap_used_kb=0)
        cols.update(kwargs)
        self.conn.execute(
            "INSERT INTO system_raw (ts, cpu_busy, load1, mem_used_kb, mem_comp_kb, "
            "swap_used_kb, disk_free_kb, samples, expected) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, cols["cpu_busy"], cols["load1"], cols["mem_used_kb"], cols["mem_comp_kb"],
             cols["swap_used_kb"], disk_free_kb, samples,
             config.TIERS[0].seconds // config.INTERVAL))
        self.conn.commit()

    def fine_rows(self):
        return self.conn.execute(
            "SELECT ts, cpu_avg, cpu_max, cpu_max_ts, samples FROM sample_fine "
            "ORDER BY ts").fetchall()


class TestBuckets(Base):
    def test_bucket_start_floors_to_the_interval(self):
        self.assertEqual(rollup.bucket_start(1000, 300), 900)
        self.assertEqual(rollup.bucket_start(900, 300), 900)


class TestCollapse(Base):
    def test_a_spike_survives_as_the_max(self):
        # Ten samples, the 61% spike in the middle of the bucket -- not
        # last, so a "last row wins" implementation cannot pass by luck.
        for i in range(10):
            self.raw(i * 30, 610 if i == 4 else 10)
        rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now=30 * DAY)
        rows = self.fine_rows()
        self.assertEqual(max(r[2] for r in rows), 610)

    def test_the_average_is_sample_weighted_not_an_average_of_averages(self):
        # One bucket carrying 9 samples at 10, one carrying 1 sample at 610.
        self.raw(0, 10, samples=9)
        self.raw(30, 610, samples=1)
        rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now=30 * DAY)
        avg = self.fine_rows()[0][1]
        self.assertEqual(avg, (10 * 9 + 610 * 1) // 10)   # 70, not 310
        self.assertNotEqual(avg, (10 + 610) // 2)

    def test_the_minute_of_the_peak_is_carried_forward(self):
        self.raw(0, 10)
        self.raw(30, 610, cpu_max_ts=30)
        self.raw(60, 10)
        rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now=30 * DAY)
        self.assertEqual(self.fine_rows()[0][3], 30)

    def test_samples_accumulate_so_the_next_tier_can_weight_correctly(self):
        self.raw(0, 10, samples=3)
        self.raw(30, 20, samples=4)
        rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now=30 * DAY)
        self.assertEqual(self.fine_rows()[0][4], 7)

    def test_source_rows_are_deleted(self):
        self.raw(0, 10)
        rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now=30 * DAY)
        left = self.conn.execute("SELECT COUNT(*) FROM sample_raw").fetchone()[0]
        self.assertEqual(left, 0)

    def test_rows_inside_the_retention_window_are_untouched(self):
        now = 30 * DAY
        self.raw(now - 60, 10)          # one minute old
        moved = rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now)
        self.assertEqual(moved, 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sample_raw").fetchone()[0], 1)

    def test_collapse_is_idempotent(self):
        # Covers a same-tick crash-and-retry: the source set replayed on the
        # second call is byte-identical to the first (the delete never
        # committed). This does NOT cover a coarser bucket assembled from
        # two DIFFERENT partial source sets across separate ticks -- that
        # case is pinned by
        # test_a_coarser_bucket_split_across_ticks_keeps_its_peak below.
        self.raw(0, 10)
        self.raw(30, 610)
        rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now=30 * DAY)
        first = self.fine_rows()
        self.raw(0, 10)   # a crash-and-retry replays the same source rows
        self.raw(30, 610)
        rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now=30 * DAY)
        self.assertEqual(self.fine_rows(), first)

    def test_a_batch_is_bounded_so_a_tick_never_stalls(self):
        for i in range(config.ROLLUP_BATCH * 2 + 50):
            self.raw(i * config.TIERS[1].seconds, 10)
        moved = rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now=400 * DAY)
        self.assertEqual(moved, config.ROLLUP_BATCH)  # progress, not just a ceiling

    def test_a_backlog_larger_than_one_batch_is_deferred_not_destroyed(self):
        # collapse() stops at ROLLUP_BATCH. Pruning with the same cutoff on
        # the same tick would delete the remainder before it is rolled up.
        fine = config.TIERS[1].seconds
        total = config.ROLLUP_BATCH + 50
        for b in range(total):
            self.raw(b * fine, 100)
        rollup.run(self.conn, now=total * fine + 8 * DAY)
        rolled = self.conn.execute("SELECT COUNT(*) FROM sample_fine").fetchone()[0]
        left = self.conn.execute(
            "SELECT COUNT(DISTINCT ts / ?) FROM sample_raw", (fine,)).fetchone()[0]
        self.assertEqual(rolled, config.ROLLUP_BATCH)
        self.assertEqual(left, 50)

    def test_repeated_runs_drain_a_backlog_completely(self):
        # Forward progress: deferring must not mean never finishing, and no
        # sample can vanish (or be double-counted) along the way -- an
        # overwrite-instead-of-merge bug would still drain the backlog while
        # silently losing samples, so count(*) > 0 alone would not catch it.
        fine = config.TIERS[1].seconds
        total = config.ROLLUP_BATCH + 50
        for b in range(total):
            self.raw(b * fine, 100)
        now = total * fine + 8 * DAY
        for _ in range(5):
            rollup.run(self.conn, now)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sample_raw").fetchone()[0], 0)
        total_samples = sum(
            self.conn.execute(
                "SELECT COALESCE(SUM(samples), 0) FROM sample_%s" % t.name).fetchone()[0]
            for t in config.TIERS)
        self.assertEqual(total_samples, total)

    def test_a_coarser_bucket_split_across_ticks_keeps_its_peak(self):
        # 200 fine buckets is 16*12 + 8, so one coarse bucket straddles the
        # batch boundary. Overwriting instead of merging erased the spike.
        fine, coarse = config.TIERS[1].seconds, config.TIERS[2].seconds
        spike_at = 16 * 12 + 3
        for b in range(400):
            self.raw(b * fine, 990 if b == spike_at else 10)
        now = 400 * fine + 40 * DAY
        for _ in range(12):
            rollup.run(self.conn, now)
        peak = self.conn.execute(
            "SELECT MAX(cpu_max) FROM (SELECT cpu_max FROM sample_fine UNION ALL "
            "SELECT cpu_max FROM sample_coarse UNION ALL "
            "SELECT cpu_max FROM sample_archive)").fetchone()[0]
        self.assertEqual(peak, 990)

    def test_rss_avg_is_sample_weighted(self):
        self.raw(0, 10, samples=9, rss=1000)
        self.raw(30, 10, samples=1, rss=10000)
        rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now=30 * DAY)
        rss_avg = self.conn.execute("SELECT rss_avg FROM sample_fine").fetchone()[0]
        self.assertEqual(rss_avg, (1000 * 9 + 10000 * 1) // 10)
        self.assertNotEqual(rss_avg, (1000 + 10000) // 2)

    def test_rss_max_is_the_true_max_not_averaged(self):
        self.raw(0, 10, rss=1000, rss_max=1000)
        self.raw(30, 10, rss=2000, rss_max=50000)
        rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now=30 * DAY)
        rss_max = self.conn.execute("SELECT rss_max FROM sample_fine").fetchone()[0]
        self.assertEqual(rss_max, 50000)

    def test_nproc_takes_the_max_not_the_sum(self):
        self.raw(0, 10, nproc=2)
        self.raw(30, 10, nproc=3)
        rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now=30 * DAY)
        nproc = self.conn.execute("SELECT nproc FROM sample_fine").fetchone()[0]
        self.assertEqual(nproc, 3)
        self.assertNotEqual(nproc, 5)

    def test_system_expected_reflects_a_full_target_bucket(self):
        self.system(0)
        rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now=30 * DAY)
        expected = self.conn.execute("SELECT expected FROM system_fine").fetchone()[0]
        self.assertEqual(expected, config.TIERS[1].seconds // config.INTERVAL)

    def test_system_samples_reflect_true_coverage_not_a_full_bucket(self):
        # Only 3 of the samples a full bucket implies are present -- e.g.
        # after a sleep gap. samples must record that truthfully so Task 11
        # can render the gap as a hatched band rather than a low value.
        self.system(0)
        self.system(30)
        self.system(60)
        rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now=30 * DAY)
        row = self.conn.execute(
            "SELECT samples, expected FROM system_fine").fetchone()
        self.assertEqual(row[0], 3)
        self.assertLess(row[0], row[1])

    def test_disk_free_kb_takes_the_min_not_the_average(self):
        self.system(0, disk_free_kb=5000)
        self.system(30, disk_free_kb=1000)
        rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now=30 * DAY)
        disk_free = self.conn.execute("SELECT disk_free_kb FROM system_fine").fetchone()[0]
        self.assertEqual(disk_free, 1000)

    def test_a_bucket_with_only_system_rows_is_not_orphaned(self):
        # No process was sampled in this bucket (no sample_raw row), but
        # system_raw still has one. Boundary selection driven only by
        # sample_<tier> would miss it, and prune_tier would then delete it
        # unrolled -- a silent hole in the samples/expected coverage signal.
        self.system(0, disk_free_kb=4321)
        rollup.run(self.conn, now=30 * DAY)
        left = self.conn.execute("SELECT COUNT(*) FROM system_raw").fetchone()[0]
        fine_row = self.conn.execute("SELECT disk_free_kb FROM system_fine").fetchone()
        self.assertEqual(left, 0)
        self.assertIsNotNone(fine_row)
        self.assertEqual(fine_row[0], 4321)


class TestPrune(Base):
    def test_the_last_tier_is_never_pruned(self):
        self.conn.execute(
            "INSERT INTO sample_archive (ts, proc_id, cpu_avg, cpu_max, cpu_max_ts, "
            "rss_avg, rss_max, nproc, samples) VALUES (0,?,10,10,0,100,100,1,1)",
            (self.pid,))
        self.conn.commit()
        rollup.prune(self.conn, now=10 * 365 * DAY)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sample_archive").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
