import unittest
from procwatch import config, db, query

DAY = 86400


class TestPickTier(unittest.TestCase):
    def test_a_short_window_uses_the_finest_tier(self):
        self.assertEqual(query.pick_tier(3600).name, "raw")

    def test_a_year_long_window_uses_a_coarse_tier(self):
        self.assertIn(query.pick_tier(365 * DAY).name, ("coarse", "archive"))

    def test_wider_windows_never_pick_a_finer_tier(self):
        spans = [3600, DAY, 10 * DAY, 100 * DAY, 1000 * DAY]
        seconds = [query.pick_tier(s).seconds for s in spans]
        self.assertEqual(seconds, sorted(seconds))


class TestSeries(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)
        self.a = self.conn.execute(
            "INSERT INTO proc (exe, args_sig, cmdline_full) VALUES ('a','','a')").lastrowid
        self.b = self.conn.execute(
            "INSERT INTO proc (exe, args_sig, cmdline_full) VALUES (?,'',?)",
            (config.OTHER, config.OTHER)).lastrowid
        for ts in range(0, 300, 30):
            self.conn.executemany(
                "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, cpu_max_ts, "
                "rss_avg, rss_max, nproc, samples) VALUES (?,?,?,?,?,?,?,?,?)",
                [(ts, self.a, 100, 610, ts, 500, 500, 3, 1),
                 (ts, self.b, 50, 50, ts, 100, 100, 9, 1)])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_series_returns_both_avg_and_max_per_point(self):
        result = query.series(self.conn, 0, 300, limit=10)
        point = result["series"][0]["points"][0]
        self.assertIn("cpu_avg", point)
        self.assertIn("cpu_max", point)
        self.assertNotEqual(point["cpu_avg"], point["cpu_max"])

    def test_percentages_are_returned_unscaled(self):
        result = query.series(self.conn, 0, 300, limit=10)
        by_name = {s["exe"]: s for s in result["series"]}
        self.assertAlmostEqual(by_name["a"]["points"][0]["cpu_max"], 61.0)

    def test_processes_below_the_cut_are_not_dropped(self):
        """The stack has to add up to every process, not to the top `limit`.

        The sampler keeps forty identities per tick. Returning only the top
        few and calling the rest absent made the chart understate the machine
        by whatever those ranks were using -- and the energy chart, which
        normalises by the sum of what it is handed, reported shares of a
        subset as shares of the whole.
        """
        extra = []
        for i in range(6):
            pid = self.conn.execute(
                "INSERT INTO proc (exe, args_sig, cmdline_full) VALUES (?,'',?)",
                ("p%d" % i, "p%d" % i)).lastrowid
            extra.append(pid)
        for ts in range(0, 300, 30):
            self.conn.executemany(
                "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, cpu_max_ts, "
                "rss_avg, rss_max, nproc, samples) VALUES (?,?,?,?,?,?,?,?,?)",
                [(ts, pid, 70, 70, ts, 10, 10, 1, 1) for pid in extra])
        self.conn.commit()

        everything = 100 + 50 + 70 * len(extra)          # tenths of a percent
        for limit in (1, 2, 4, 99):
            result = query.series(self.conn, 0, 300, limit=limit)
            got = sum(p["cpu_avg"] for s in result["series"]
                      for p in s["points"] if p["ts"] == 0)
            self.assertAlmostEqual(got, everything / 10.0, places=6,
                                   msg="limit=%d lost work" % limit)

    def _busy_other_and_a_crowd(self):
        """Make the sampler's __other__ row outrank a real process, and leave
        real processes below the cut as well.

        Both conditions are needed: the first is what lets the stored
        remainder win a rank, the second is what makes a second remainder
        exist to collide with it.
        """
        self.conn.execute("UPDATE sample_raw SET cpu_max = 9000 WHERE proc_id = ?",
                          (self.b,))
        small = []
        for i in range(4):
            pid = self.conn.execute(
                "INSERT INTO proc (exe, args_sig, cmdline_full) VALUES (?,'',?)",
                ("small%d" % i, "small%d" % i)).lastrowid
            small.append(pid)
        for ts in range(0, 300, 30):
            self.conn.executemany(
                "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, cpu_max_ts, "
                "rss_avg, rss_max, nproc, samples) VALUES (?,?,?,?,?,?,?,?,?)",
                # cpu_max far above cpu_avg: a brief spike on top of a quiet
                # average, which is what distinguishes a summed remainder from
                # one that reports the largest peak underneath it.
                [(ts, pid, 20, 800, ts, 10, 10, 1, 1) for pid in small])
        self.conn.commit()

    def test_there_is_exactly_one_remainder_however_many_rank(self):
        # The sampler writes its own __other__ row. If that row could also win
        # a rank there would be two bands both claiming to be the remainder,
        # and the reader has no way to tell which is which.
        self._busy_other_and_a_crowd()
        for limit in (1, 2, 3):
            result = query.series(self.conn, 0, 300, limit=limit)
            others = [s for s in result["series"] if s["is_other"]]
            self.assertEqual(len(others), 1, "limit=%d gave %d remainders"
                             % (limit, len(others)))

    def test_the_remainder_claims_no_peak_it_cannot_justify(self):
        # It is a sum over many processes whose maxima did not co-occur, so
        # any peak it reported would be a number nothing ever reached. The
        # crowd below the cut each spike to 80% while averaging 2%.
        self._busy_other_and_a_crowd()
        result = query.series(self.conn, 0, 300, limit=1)
        other = [s for s in result["series"] if s["is_other"]][0]
        biggest = max(p["cpu_max"] for p in other["points"])
        self.assertLess(biggest, 80.0,
                        "the remainder is reporting a peak from one process")
        for point in other["points"]:
            self.assertAlmostEqual(point["cpu_max"], point["cpu_avg"])

    def test_the_other_row_is_present_and_marked(self):
        result = query.series(self.conn, 0, 300, limit=10)
        other = [s for s in result["series"] if s["is_other"]]
        self.assertEqual(len(other), 1)

    def test_cpu_max_ts_names_the_peak_within_the_bucket(self):
        # cpu_max_ts is set equal to ts by setUp's fixture; assert it round
        # trips distinctly per point rather than being dropped or constant.
        result = query.series(self.conn, 0, 300, limit=10)
        by_name = {s["exe"]: s for s in result["series"]}
        points = by_name["a"]["points"]
        self.assertEqual([p["cpu_max_ts"] for p in points], [p["ts"] for p in points])

    def test_tier_picked_for_the_requested_span_is_reported(self):
        result = query.series(self.conn, 0, 300, limit=10)
        self.assertEqual(result["tier"], query.pick_tier(300).name)

    def test_bucket_detail_is_ranked_and_carries_the_process_count(self):
        rows = query.bucket_detail(self.conn, "raw", 60)
        self.assertEqual(rows[0]["exe"], "a")
        self.assertEqual(rows[0]["nproc"], 3)

    def test_bucket_detail_names_the_minute_each_process_peaked(self):
        # The drill-down is where "which minute did it peak" is the question,
        # and it is asked hardest of a six-hour archive bucket. Dropping
        # cpu_max_ts here would defeat the tier design where it pays off most.
        rows = query.bucket_detail(self.conn, "raw", 60)
        top = rows[0]
        self.assertIn("cpu_max_ts", top)
        self.assertEqual(top["cpu_max_ts"], 60)
        self.assertGreaterEqual(top["cpu_max"], top["cpu_avg"])

    def test_bucket_detail_marks_the_other_row(self):
        rows = query.bucket_detail(self.conn, "raw", 60)
        by_name = {r["exe"]: r for r in rows}
        self.assertTrue(by_name[config.OTHER]["is_other"])
        self.assertFalse(by_name["a"]["is_other"])

    def test_bucket_detail_rejects_an_unknown_tier(self):
        # tier_name can arrive from an HTTP query parameter; a bogus value
        # must raise, not be interpolated into a table name.
        with self.assertRaises(ValueError):
            query.bucket_detail(self.conn, "'; DROP TABLE proc; --", 60)

    def test_a_window_wider_than_the_finest_tier_still_returns_recent_data(self):
        # Tiers are disjoint in time -- a row lives in raw until it expires,
        # then moves to fine, and so on. Selecting a single table by span
        # width alone (as if every tier held a copy of the same history)
        # returned nothing for any window not matching wherever the data
        # currently lives. All this fixture's rows are in sample_raw;
        # a day-wide window renders at "fine" resolution but must still
        # find them.
        self.conn.execute(
            "INSERT INTO system_raw (ts, cpu_busy, load1, mem_used_kb, mem_comp_kb, "
            "swap_used_kb, disk_free_kb, samples, expected) "
            "VALUES (0, 100, 100, 1000, 0, 0, 5000, 1, 1)")
        self.conn.commit()

        result = query.series(self.conn, 0, 86400, limit=10)
        self.assertEqual(result["tier"], "fine")
        self.assertTrue(result["series"])
        self.assertTrue(result["system"])

        by_name = {s["exe"]: s for s in result["series"]}
        points = by_name["a"]["points"]
        # All 10 of proc a's raw samples (ts 0..270, all cpu_avg=100,
        # cpu_max=610) fall inside one 300-second render bucket.
        self.assertEqual(len(points), 1)
        self.assertAlmostEqual(points[0]["cpu_avg"], 10.0)
        self.assertAlmostEqual(points[0]["cpu_max"], 61.0)
        self.assertEqual(points[0]["cpu_max_ts"], 0)

    def test_a_window_spanning_two_tiers_returns_both(self):
        # sample_fine holds already-rolled-up data (samples > 1) further
        # back in time than sample_raw's window. A query spanning both must
        # surface points sourced from each table in their own render
        # buckets -- not merged into one and not dropped.
        self.conn.execute(
            "INSERT INTO sample_fine (ts, proc_id, cpu_avg, cpu_max, cpu_max_ts, "
            "rss_avg, rss_max, nproc, samples) VALUES (97200, ?, 200, 700, 97200, "
            "600, 600, 2, 5)", (self.a,))
        self.conn.commit()

        result = query.series(self.conn, 0, 200000, limit=10)
        self.assertEqual(result["tier"], "coarse")
        by_name = {s["exe"]: s for s in result["series"]}
        points = sorted(by_name["a"]["points"], key=lambda p: p["ts"])
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0]["ts"], 0)
        self.assertAlmostEqual(points[0]["cpu_max"], 61.0)   # sourced from sample_raw
        self.assertEqual(points[1]["ts"], 97200)
        self.assertAlmostEqual(points[1]["cpu_max"], 70.0)   # sourced from sample_fine

    def test_cpu_avg_fold_is_sample_weighted_not_a_naive_mean(self):
        # Two rows landing in the same render bucket with very different
        # sample counts -- guaranteed whenever a raw row (samples=1) and an
        # already-rolled-up row (samples>1) share a bucket. A naive mean of
        # the two stored averages would be wrong; rollup.collapse weights by
        # samples, and this read-side fold has to match it.
        c = self.conn.execute(
            "INSERT INTO proc (exe, args_sig, cmdline_full) VALUES ('c','','c')").lastrowid
        self.conn.executemany(
            "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, cpu_max_ts, "
            "rss_avg, rss_max, nproc, samples) VALUES (?,?,?,?,?,?,?,?,?)",
            [(1000, c, 100, 100, 1000, 0, 0, 1, 1),
             (1010, c, 500, 500, 1010, 0, 0, 1, 9)])
        self.conn.commit()
        result = query.series(self.conn, 990, 1020, limit=10)
        by_name = {s["exe"]: s for s in result["series"]}
        point = by_name["c"]["points"][0]
        # weighted: (100*1 + 500*9) / 10 = 460 -> 46.0%.
        # naive mean would give (100 + 500) / 2 = 300 -> 30.0%.
        self.assertAlmostEqual(point["cpu_avg"], 46.0)

    def test_bucket_detail_reads_across_tiers_for_its_bucket_width(self):
        # tier_name says which resolution to drill into, not which table
        # holds the data -- the rows for a "fine"-resolution bucket may
        # still live in sample_raw if they have not expired upward yet.
        # This fixture's rows are all in sample_raw; drilling in at "fine"
        # (a 300-second bucket covering all of them) must still find them.
        rows = query.bucket_detail(self.conn, "fine", 0)
        self.assertEqual(rows[0]["exe"], "a")
        self.assertEqual(rows[0]["nproc"], 3)


class TestSystemAndGaps(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_system_points_are_unscaled_and_carry_coverage(self):
        self.conn.execute(
            "INSERT INTO system_raw (ts, cpu_busy, load1, mem_used_kb, mem_comp_kb, "
            "swap_used_kb, disk_free_kb, samples, expected) "
            "VALUES (0, 305, 150, 1000, 200, 0, 5000, 3, 10)")
        self.conn.commit()
        result = query.series(self.conn, 0, 60, limit=10)
        point = result["system"][0]
        self.assertAlmostEqual(point["cpu_busy"], 30.5)
        self.assertAlmostEqual(point["load1"], 1.5)
        # At raw resolution a bucket spans one interval and so expects one
        # sample. Coverage is about wall clock the bucket covers, not about
        # whatever count a source row happens to carry.
        self.assertAlmostEqual(point["coverage"], 1.0)

    def _system_row(self, ts):
        self.conn.execute(
            "INSERT INTO system_raw (ts, cpu_busy, load1, mem_used_kb, mem_comp_kb, "
            "swap_used_kb, disk_free_kb, samples, expected) "
            "VALUES (?, 0, 0, 0, 0, 0, 0, 1, 1)", (ts,))

    def test_a_mostly_asleep_bucket_reads_as_a_gap_not_a_quiet_period(self):
        # Two ticks inside a five-minute render bucket that could hold ten.
        # Coverage must reflect the wall clock the bucket covers, not the
        # expectation of the rows that happen to be present -- otherwise a
        # machine asleep for four of five minutes reads fully covered, and
        # the sub-0.5 hatching never fires for the case it exists for.
        for ts in (0, 30):
            self._system_row(ts)
        self.conn.commit()
        point = query.series(self.conn, 0, 86400, limit=10)["system"][0]
        self.assertAlmostEqual(point["coverage"], 0.2)
        self.assertLess(point["coverage"], 0.5)

    def test_a_fully_covered_bucket_reads_as_covered(self):
        for i in range(10):
            self._system_row(i * config.INTERVAL)
        self.conn.commit()
        point = query.series(self.conn, 0, 86400, limit=10)["system"][0]
        self.assertAlmostEqual(point["coverage"], 1.0)

    def test_coverage_does_not_change_when_rollup_crosses_a_window(self):
        # The same two ticks, once as raw rows and once already rolled into a
        # fine row. Before the fix the fold summed the sources' own 'expected'
        # while rollup wrote a nominal width, so a window's hatching appeared
        # or vanished overnight depending on which side of the rollup it sat.
        for ts in (0, 30):
            self._system_row(ts)
        self.conn.commit()
        before = query.series(self.conn, 0, 86400, limit=10)["system"][0]["coverage"]

        self.conn.execute("DELETE FROM system_raw")
        self.conn.execute(
            "INSERT INTO system_fine (ts, cpu_busy, load1, mem_used_kb, mem_comp_kb, "
            "swap_used_kb, disk_free_kb, samples, expected) "
            "VALUES (0, 0, 0, 0, 0, 0, 0, 2, 10)")
        self.conn.commit()
        after = query.series(self.conn, 0, 86400, limit=10)["system"][0]["coverage"]
        self.assertAlmostEqual(before, after)

    def test_gap_reason_is_passed_through(self):
        self.conn.execute(
            "INSERT INTO gap (ts_start, ts_end, reason) VALUES (10, 20, 'sleep')")
        self.conn.commit()
        result = query.series(self.conn, 0, 60, limit=10)
        self.assertEqual(result["gaps"][0]["reason"], "sleep")

    def test_system_fold_accumulates_expected_not_just_the_first_rows_value(self):
        # Two rows in the same render bucket with different 'expected'
        # values. samples must sum (1+1=2) AND expected must sum (1+9=10)
        # for coverage to stay meaningful -- if expected were left at only
        # the first row's value, this bucket would misreport itself as
        # over 100% covered instead of mostly missing.
        self.conn.execute(
            "INSERT INTO system_raw (ts, cpu_busy, load1, mem_used_kb, mem_comp_kb, "
            "swap_used_kb, disk_free_kb, samples, expected) "
            "VALUES (0, 0, 0, 0, 0, 0, 0, 1, 1)")
        self.conn.execute(
            "INSERT INTO system_raw (ts, cpu_busy, load1, mem_used_kb, mem_comp_kb, "
            "swap_used_kb, disk_free_kb, samples, expected) "
            "VALUES (30, 0, 0, 0, 0, 0, 0, 1, 9)")
        self.conn.commit()
        # A day-wide window renders at "fine" (300s) resolution, folding
        # both rows (ts 0 and 30) into the same bucket.
        result = query.series(self.conn, 0, 86400, limit=10)
        self.assertAlmostEqual(result["system"][0]["coverage"], 0.2)


if __name__ == "__main__":
    unittest.main()
