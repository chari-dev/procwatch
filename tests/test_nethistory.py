"""Looking backwards at the network: who was talking, and how much."""
import time
import unittest

from procwatch import config, db, query, server


def _sample(conn, ts, rows):
    """Write one tick of samples.

    rows is [(exe, app, net_in, net_out, cpu)]. cpu matters even to a test
    about bytes: the remainder band is only carried when it used some CPU,
    which is how the sampler's own remainder row always arrives.
    """
    with conn:
        for exe, app, got_in, got_out, cpu in rows:
            proc = conn.execute(
                "SELECT id FROM proc WHERE exe=? AND args_sig=''",
                (exe,)).fetchone()
            if proc is None:
                cur = conn.execute(
                    "INSERT INTO proc (exe, args_sig, cmdline_full, "
                    "is_system, app) VALUES (?,'',?,0,?)", (exe, exe, app))
                proc_id = cur.lastrowid
            else:
                proc_id = proc[0]
            conn.execute(
                "INSERT INTO sample_raw (proc_id, ts, cpu_avg, cpu_max, "
                "cpu_max_ts, rss_avg, rss_max, nproc, samples, net_in, "
                "net_out, disk_read, disk_write, energy, stuck) "
                "VALUES (?,?,?,?,?,0,0,1,1,?,?,0,0,0,0)",
                (proc_id, ts, cpu, cpu, ts, got_in, got_out))


class TestHistory(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)
        self.now = int(time.time())
        for step in range(4):
            ts = self.now - 600 + step * 30
            _sample(self.conn, ts, [
                ("Arc", "Arc", 1000, 100, 40),
                ("Arc Helper", "Arc", 500, 50, 10),
                ("quiet", "", 0, 0, 5),
                (config.OTHER, "", 7, 8, 30),
            ])
        self.addCleanup(self.conn.close)

    def _ask(self, span=3600):
        return server.api_get(self.conn, "/api/nethistory",
                              {"start": [str(self.now - span)],
                               "end": [str(self.now)]})

    def test_it_reports_who_was_talking(self):
        out = self._ask()
        names = [a["app"] for a in out["apps"]]
        self.assertIn("Arc", names)
        self.assertNotIn("quiet", names)      # nothing sent, nothing to say

    def test_one_application_is_one_row(self):
        # Arc and Arc Helper are two recorded identities and one application.
        out = self._ask()
        arc = [a for a in out["apps"] if a["app"] == "Arc"]
        self.assertEqual(len(arc), 1)
        self.assertEqual(arc[0]["bytes_in"], 4 * 1500)
        self.assertEqual(arc[0]["bytes_out"], 4 * 150)

    def test_the_remainder_row_is_named_in_words(self):
        out = self._ask()
        names = [a["app"] for a in out["apps"]]
        self.assertIn("Everything else", names)
        self.assertNotIn(config.OTHER, names)

    def test_rows_carry_a_series_to_draw(self):
        arc = [a for a in self._ask()["apps"] if a["app"] == "Arc"][0]
        self.assertTrue(arc["points"])
        self.assertEqual(sorted(arc["points"][0]), ["in", "out", "ts"])
        # Ordered, so a chart drawn from them is not a scribble.
        stamps = [p["ts"] for p in arc["points"]]
        self.assertEqual(stamps, sorted(stamps))

    def test_the_busiest_talker_leads(self):
        out = self._ask()
        totals = [a["bytes_in"] + a["bytes_out"] for a in out["apps"]]
        self.assertEqual(totals, sorted(totals, reverse=True))

    def test_an_empty_window_is_empty_not_an_error(self):
        out = server.api_get(self.conn, "/api/nethistory",
                             {"start": [str(self.now - 10 ** 7)],
                              "end": [str(self.now - 10 ** 6)]})
        self.assertEqual(out["apps"], [])


class TestRanking(unittest.TestCase):
    """Ranking by bytes, which is a different order from ranking by CPU."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)
        self.now = int(time.time())
        with self.conn:
            for name, cpu, bytes_in in (("chatty", 1, 900000),
                                        ("busy", 900, 10)):
                cur = self.conn.execute(
                    "INSERT INTO proc (exe, args_sig, cmdline_full, "
                    "is_system, app) VALUES (?,'',?,0,'')", (name, name))
                self.conn.execute(
                    "INSERT INTO sample_raw (proc_id, ts, cpu_avg, cpu_max, "
                    "cpu_max_ts, rss_avg, rss_max, nproc, samples, net_in, "
                    "net_out, disk_read, disk_write, energy, stuck) "
                    "VALUES (?,?,?,?,?,0,0,1,1,?,0,0,0,0,0)",
                    (cur.lastrowid, self.now - 60, cpu, cpu, self.now - 60,
                     bytes_in))
        self.addCleanup(self.conn.close)

    def test_the_network_ranking_finds_the_talker(self):
        out = query.series(self.conn, self.now - 3600, self.now, limit=1,
                           rank="net")
        self.assertEqual([s["exe"] for s in out["series"]
                          if s["exe"] != config.OTHER], ["chatty"])

    def test_the_cpu_ranking_still_finds_the_burner(self):
        out = query.series(self.conn, self.now - 3600, self.now, limit=1)
        self.assertEqual([s["exe"] for s in out["series"]
                          if s["exe"] != config.OTHER], ["busy"])


if __name__ == "__main__":
    unittest.main()
