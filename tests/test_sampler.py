# tests/test_sampler.py
import unittest
from procwatch import config, db, sampler
from procwatch.psreader import Proc
from procwatch.system import Readings


def proc(pid, cputime_cs, rss=1000, comm="/usr/bin/thing", command=None, start=1000):
    return Proc(pid, start, cputime_cs, rss, comm, command or comm)


READINGS = Readings(100, 5000, 100, 0, 999999)


class TestCpuPercent(unittest.TestCase):
    def test_one_second_of_cpu_over_one_second_is_one_hundred_percent(self):
        self.assertAlmostEqual(sampler.cpu_percent(0, 100, 1.0), 100.0)

    def test_half_a_second_over_thirty_seconds(self):
        self.assertAlmostEqual(sampler.cpu_percent(0, 50, 30.0), 1.6667, places=3)

    def test_a_negative_delta_is_discarded(self):
        self.assertIsNone(sampler.cpu_percent(500, 100, 30.0))

    def test_a_non_positive_interval_is_discarded(self):
        # A DST or NTP correction; dividing would write a garbage spike.
        self.assertIsNone(sampler.cpu_percent(0, 100, 0.0))
        self.assertIsNone(sampler.cpu_percent(0, 100, -5.0))


class TestAggregate(unittest.TestCase):
    def test_a_first_sighting_has_no_previous_reading_so_no_rate(self):
        aggs, state = sampler.aggregate([proc(1, 500)], {}, now=100, dt=30.0)
        self.assertEqual(aggs, {})
        self.assertIn(1, state)

    def test_a_recycled_pid_does_not_borrow_the_previous_tenant_cpu_clock(self):
        prev = {1: (1000, 50000)}          # (start_time, cputime_cs)
        aggs, _ = sampler.aggregate([proc(1, 10, start=9999)], prev, now=100, dt=30.0)
        self.assertEqual(aggs, {})

    def test_pids_sharing_an_identity_have_cpu_and_rss_summed(self):
        prev = {1: (1000, 0), 2: (1000, 0)}
        procs = [proc(1, 300, rss=1000), proc(2, 600, rss=2000)]
        aggs, _ = sampler.aggregate(procs, prev, now=100, dt=30.0)
        agg = aggs[("thing", "")]
        self.assertEqual(agg.nproc, 2)
        self.assertEqual(agg.rss, 3000)
        self.assertAlmostEqual(agg.cpu, sampler.cpu_percent(0, 900, 30.0))

    def test_distinct_identities_stay_separate(self):
        prev = {1: (1000, 0), 2: (1000, 0)}
        procs = [
            proc(1, 300, comm="/usr/bin/log", command="/usr/bin/log stream --predicate A"),
            proc(2, 300, comm="/usr/bin/log", command="/usr/bin/log stream --predicate B"),
        ]
        aggs, _ = sampler.aggregate(procs, prev, now=100, dt=30.0)
        self.assertEqual(len(aggs), 2)


class TestSelect(unittest.TestCase):
    def _aggs(self, n):
        return {("p%d" % i, ""): sampler.Agg(cpu=float(i), rss=i * 10, nproc=1)
                for i in range(n)}

    def test_everything_outside_the_top_n_lands_in_other(self):
        kept, other = sampler.select(self._aggs(config.TOP_N + 15))
        self.assertLessEqual(len(kept), config.TOP_N * 2)
        self.assertGreater(other.cpu, 0)

    def test_the_kept_set_is_the_union_of_top_cpu_and_top_rss(self):
        aggs = {
            ("hungry", ""): sampler.Agg(cpu=90.0, rss=1, nproc=1),
            ("fat", ""): sampler.Agg(cpu=0.1, rss=10 ** 9, nproc=1),
        }
        kept, _ = sampler.select(aggs)
        self.assertIn(("hungry", ""), kept)
        self.assertIn(("fat", ""), kept)

    def test_nothing_is_lost_between_kept_and_other(self):
        aggs = self._aggs(config.TOP_N * 3)
        kept, other = sampler.select(aggs)
        total = sum(a.cpu for a in aggs.values())
        self.assertAlmostEqual(sum(a.cpu for a in kept.values()) + other.cpu, total, places=6)


class TestTick(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)

    def _rows(self):
        return self.conn.execute(
            "SELECT p.exe, s.cpu_avg, s.rss_avg, s.nproc FROM sample_raw s "
            "JOIN proc p ON p.id = s.proc_id").fetchall()

    def test_the_first_tick_writes_state_but_no_samples(self):
        sampler.tick(self.conn, [proc(1, 500)], READINGS, now=1000)
        self.assertEqual(self._rows(), [])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sampler_state").fetchone()[0], 1)

    def test_the_second_tick_writes_a_sample(self):
        sampler.tick(self.conn, [proc(1, 0)], READINGS, now=1000)
        sampler.tick(self.conn, [proc(1, 300)], READINGS, now=1030)
        rows = self._rows()
        self.assertEqual(len(rows), 2)      # the process plus __other__
        exes = {r[0] for r in rows}
        self.assertIn("thing", exes)
        self.assertIn(config.OTHER, exes)

    def test_a_sample_row_records_avg_equal_to_max_at_raw_resolution(self):
        sampler.tick(self.conn, [proc(1, 0)], READINGS, now=1000)
        sampler.tick(self.conn, [proc(1, 300)], READINGS, now=1030)
        avg, mx, samples = self.conn.execute(
            "SELECT cpu_avg, cpu_max, samples FROM sample_raw s JOIN proc p "
            "ON p.id = s.proc_id WHERE p.exe = 'thing'").fetchone()
        self.assertEqual(avg, mx)
        self.assertEqual(samples, 1)

    def test_a_long_gap_is_recorded_and_its_delta_discarded(self):
        sampler.tick(self.conn, [proc(1, 0)], READINGS, now=1000)
        sampler.tick(self.conn, [proc(1, 999999)], READINGS, now=1000 + 8 * 3600)
        gaps = self.conn.execute("SELECT ts_start, ts_end, reason FROM gap").fetchall()
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0][2], "sleep")
        self.assertEqual(self._rows(), [])

    def test_identities_are_interned_not_duplicated(self):
        for i, cputime in enumerate([0, 300, 600]):
            sampler.tick(self.conn, [proc(1, cputime)], READINGS, now=1000 + i * 30)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM proc WHERE exe = 'thing'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_a_pid_absent_for_one_tick_does_not_inflate_the_next_rate(self):
        # A pid missing from one tick used to leave a two-tick-old cputime
        # baseline that got divided by a one-tick dt -- exactly 2x inflation.
        # Pinning _load_state to the newest tick's rows is what prevents it.
        sampler.tick(self.conn, [proc(1, 0), proc(2, 0)], READINGS, now=1000)
        sampler.tick(self.conn, [proc(1, 150)], READINGS, now=1030)
        sampler.tick(self.conn, [proc(1, 300), proc(2, 300)], READINGS, now=1060)
        cpu = self.conn.execute(
            "SELECT s.cpu_avg FROM sample_raw s JOIN proc p ON p.id = s.proc_id "
            "WHERE p.exe = 'thing' AND s.ts = 1060").fetchone()[0]
        self.assertEqual(cpu, 50)   # 5.0%, not the 15.0% the old baseline gave

    def test_a_backwards_clock_step_is_recorded_as_a_gap(self):
        sampler.tick(self.conn, [proc(1, 0)], READINGS, now=10000)
        sampler.tick(self.conn, [proc(1, 150)], READINGS, now=10030)
        self.conn.execute("UPDATE sampler_state SET updated_ts = ?", (10030 + 3600,))
        self.conn.commit()
        sampler.tick(self.conn, [proc(1, 300)], READINGS, now=10060)
        rows = self.conn.execute(
            "SELECT ts_start, ts_end, reason FROM gap").fetchall()
        self.assertEqual(len(rows), 1)
        ts_start, ts_end, reason = rows[0]
        self.assertEqual(reason, "clock")
        # The normalisation this pins: before it the row was stored
        # (13630, 10060), which a range query zoomed inside the affected
        # window would miss and which makes duration arithmetic negative.
        self.assertLess(ts_start, ts_end)
        self.assertEqual((ts_start, ts_end), (10060, 13630))

    def test_a_sleep_gap_keeps_its_ordering_too(self):
        sampler.tick(self.conn, [proc(1, 0)], READINGS, now=3000)
        sampler.tick(self.conn, [proc(1, 100)], READINGS, now=3000 + 8 * 3600)
        ts_start, ts_end, reason = self.conn.execute(
            "SELECT ts_start, ts_end, reason FROM gap").fetchone()
        self.assertEqual(reason, "sleep")
        self.assertLess(ts_start, ts_end)

    def test_the_stored_command_line_is_the_real_one_not_the_masked_signature(self):
        # watchlist.pattern is matched against cmdline_full, so storing the
        # masked form would make a pattern like "--port 8080" unmatchable.
        raw = "/usr/bin/thing --port 8080"
        p = lambda cs: Proc(1, 1000, cs, 1000, "/usr/bin/thing", raw)
        sampler.tick(self.conn, [p(0)], READINGS, now=2000)
        sampler.tick(self.conn, [p(300)], READINGS, now=2030)
        stored = self.conn.execute(
            "SELECT cmdline_full FROM proc WHERE exe = 'thing'").fetchone()[0]
        self.assertEqual(stored, raw)
        self.assertIn("8080", stored)


if __name__ == "__main__":
    unittest.main()
