"""Ranking by what a process spent, not by what it peaked at."""
import unittest

from procwatch import db, query, server


def _machine():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    base = 1750000000
    with conn:
        # Spiky peaks high and spends nothing; Steady never peaks and spends
        # everything. This is the real shape: a compile spikes a core for ten
        # seconds, a sync daemon sips for hours and costs the battery more.
        for pid, exe in ((1, "Spiky"), (2, "Steady"), (3, "Idle"), (4, "Burst")):
            conn.execute("INSERT INTO proc (id, exe, args_sig, cmdline_full, "
                         "app, is_system) VALUES (?,?,'','',?,0)",
                         (pid, exe, exe))
        for i in range(20):
            ts = base + i * 30
            conn.execute(
                "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, "
                "cpu_max_ts, rss_avg, rss_max, nproc, samples, energy) "
                "VALUES (?,1,50,9000,?,1000,1000,1,1,1)", (ts, ts))
            conn.execute(
                "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, "
                "cpu_max_ts, rss_avg, rss_max, nproc, samples, energy) "
                "VALUES (?,2,40,60,?,1000,1000,1,1,5000)", (ts, ts))
            conn.execute(
                "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, "
                "cpu_max_ts, rss_avg, rss_max, nproc, samples, energy) "
                "VALUES (?,3,1,2,?,1000,1000,1,1,0)", (ts, ts))
            # One enormous interval and nothing else: a bigger peak than
            # Steady ever reaches, and a smaller total. Ranking by MAX(energy)
            # rather than SUM would pick this, and picking it would mean the
            # chart named the process that flared rather than the one that
            # actually drained the battery.
            conn.execute(
                "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, "
                "cpu_max_ts, rss_avg, rss_max, nproc, samples, energy) "
                "VALUES (?,4,2,4,?,1000,1000,1,1,?)",
                (ts, ts, 90000 if i == 7 else 0))
            conn.execute(
                "INSERT INTO system_raw (ts, cpu_busy, load1, mem_used_kb, "
                "mem_comp_kb, swap_used_kb, disk_free_kb, samples, expected, "
                "batt_pct, batt_full_mwh, on_ac) "
                "VALUES (?,900,100,8000000,500000,0,100000000,1,1,?,50000,0)",
                (ts, 80 - i // 4))
    return conn, base, base + 20 * 30


class TestRanking(unittest.TestCase):
    def test_the_default_ranking_is_by_cpu_peak(self):
        conn, start, end = _machine()
        out = query.series(conn, start, end, limit=1)
        self.assertEqual([s["exe"] for s in out["series"] if not s["is_other"]],
                         ["Spiky"])

    def test_ranking_by_energy_picks_the_spender_instead(self):
        conn, start, end = _machine()
        out = query.series(conn, start, end, limit=1, rank="energy")
        self.assertEqual([s["exe"] for s in out["series"] if not s["is_other"]],
                         ["Steady"])

    def test_an_unknown_ranking_falls_back_rather_than_failing(self):
        # It arrives from a query string. A typo should cost the caller the
        # ranking they asked for, not the whole chart.
        conn, start, end = _machine()
        out = query.series(conn, start, end, limit=1, rank="wibble")
        self.assertEqual([s["exe"] for s in out["series"] if not s["is_other"]],
                         ["Spiky"])

    def test_a_single_flare_does_not_outrank_a_steady_drain(self):
        # SUM, not MAX. Burst's one interval is bigger than any of Steady's and
        # its total is smaller, so the two measures disagree -- and the one that
        # answers "what spent my battery" is the total.
        conn, start, end = _machine()
        out = query.series(conn, start, end, limit=2, rank="energy")
        named = [s["exe"] for s in out["series"] if not s["is_other"]]
        self.assertEqual(named[0], "Steady")

    def test_the_remainder_band_is_still_produced(self):
        # The share denominator is the sum of what the chart was given, so a
        # missing remainder would inflate every share.
        conn, start, end = _machine()
        out = query.series(conn, start, end, limit=1, rank="energy")
        self.assertTrue([s for s in out["series"] if s["is_other"]])


class TestEndpoint(unittest.TestCase):
    def _get(self, extra):
        conn, start, end = _machine()
        params = {"start": [str(start)], "end": [str(end)], "limit": ["1"]}
        params.update(extra)
        return server.api_get(conn, "/api/series", params)

    def test_the_second_ranking_is_sent_when_asked_for(self):
        out = self._get({"energy": ["1"]})
        self.assertIn("energy_series", out)
        named = [s["exe"] for s in out["energy_series"] if not s["is_other"]]
        self.assertEqual(named, ["Steady"])

    def test_it_is_not_sent_otherwise(self):
        # Every other caller -- a peer, an export, a phone -- pays nothing for
        # a chart it is not drawing.
        self.assertNotIn("energy_series", self._get({}))

    def test_the_two_rankings_disagree_which_is_the_whole_point(self):
        out = self._get({"energy": ["1"]})
        by_cpu = [s["exe"] for s in out["series"] if not s["is_other"]]
        by_energy = [s["exe"] for s in out["energy_series"] if not s["is_other"]]
        self.assertNotEqual(by_cpu, by_energy)

    def test_both_rankings_describe_the_same_buckets(self):
        # The battery readings are drawn from `system`, and the bands from
        # `energy_series`. If they were fetched separately they could straddle a
        # tick and disagree about the last bucket.
        out = self._get({"energy": ["1"]})
        stamps = {p["ts"] for s in out["energy_series"] for p in s["points"]}
        self.assertTrue(stamps.issubset({r["ts"] for r in out["system"]}))

    def test_a_falsy_value_does_not_turn_it_on(self):
        for value in ("0", "false", ""):
            self.assertNotIn("energy_series", self._get({"energy": [value]}))


if __name__ == "__main__":
    unittest.main()
