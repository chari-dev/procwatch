import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from procwatch import db, netpeer


def socket_row(app, remote, host, got_in, got_out, local="10.0.0.2:5000"):
    return {"name": app, "conns": [{"proto": "tcp4", "local": local,
                                    "remote": remote, "host": host,
                                    "bytes_in": got_in, "bytes_out": got_out}]}


class NetPeerTest(unittest.TestCase):
    def setUp(self):
        # The whole schema, not just netpeer's own tables: peers() reads the
        # interned identities to decide what counts as part of macOS, and a
        # test database that omits them is not the database this runs against.
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)
        self.now = 1_700_000_000

    def tearDown(self):
        self.conn.close()

    def rows(self):
        return self.conn.execute(
            "SELECT ts, ip, app, bytes_in, bytes_out FROM net_peer "
            "ORDER BY ip, app").fetchall()

    def test_first_sighting_records_nothing(self):
        """A cumulative counter with nothing to difference against is not a
        measurement; recording it would credit a socket's whole lifetime to
        the bucket we happened to notice it in."""
        netpeer.record(self.conn, [socket_row("Arc", "1.2.3.4:443", "1.2.3.4",
                                              5000, 900)], self.now)
        self.assertEqual(self.rows(), [])

    def test_second_sighting_records_the_delta(self):
        rows = [socket_row("Arc", "1.2.3.4:443", "1.2.3.4", 5000, 900)]
        netpeer.record(self.conn, rows, self.now)
        rows = [socket_row("Arc", "1.2.3.4:443", "1.2.3.4", 6500, 1100)]
        netpeer.record(self.conn, rows, self.now + 30)
        self.assertEqual(self.rows(),
                         [(netpeer.bucket_of(self.now), "1.2.3.4", "Arc",
                           1500, 200)])

    def test_deltas_accumulate_inside_one_bucket(self):
        for i, (got_in, got_out) in enumerate([(100, 10), (300, 30), (600, 60)]):
            netpeer.record(self.conn,
                           [socket_row("Arc", "1.2.3.4:443", "1.2.3.4",
                                       got_in, got_out)],
                           self.now + i * 30)
        # 200 + 300 gained after the first sighting established the baseline.
        self.assertEqual(self.rows(),
                         [(netpeer.bucket_of(self.now), "1.2.3.4", "Arc",
                           500, 50)])

    def test_a_new_bucket_starts_a_new_row(self):
        netpeer.record(self.conn, [socket_row("Arc", "1.2.3.4:443", "1.2.3.4",
                                              100, 10)], self.now)
        netpeer.record(self.conn, [socket_row("Arc", "1.2.3.4:443", "1.2.3.4",
                                              400, 40)], self.now + 30)
        later = self.now + netpeer.BUCKET + 30
        netpeer.record(self.conn, [socket_row("Arc", "1.2.3.4:443", "1.2.3.4",
                                              900, 90)], later)
        self.assertEqual(len(self.rows()), 2)

    def test_a_long_download_is_not_recounted_every_bucket(self):
        """The bug this differencing exists to prevent: summing raw cumulative
        counters reports the whole transfer again in every bucket."""
        total = 0
        got = 1000
        netpeer.record(self.conn, [socket_row("Arc", "1.2.3.4:443", "1.2.3.4",
                                              got, 0)], self.now)
        for i in range(1, 20):
            got += 1000
            netpeer.record(self.conn,
                           [socket_row("Arc", "1.2.3.4:443", "1.2.3.4", got, 0)],
                           self.now + i * 30)
        total = sum(r[3] for r in self.rows())
        self.assertEqual(total, 19_000)

    def test_a_counter_going_backwards_is_discarded(self):
        """A reused local port is a different socket; its total is not ours."""
        netpeer.record(self.conn, [socket_row("Arc", "1.2.3.4:443", "1.2.3.4",
                                              9000, 0)], self.now)
        netpeer.record(self.conn, [socket_row("Arc", "1.2.3.4:443", "1.2.3.4",
                                              50, 0)], self.now + 30)
        self.assertEqual(self.rows(), [])

    def test_two_sockets_to_one_host_are_differenced_separately(self):
        rows = [{"name": "Arc", "conns": [
            {"proto": "tcp4", "local": "10.0.0.2:5000", "remote": "1.2.3.4:443",
             "host": "1.2.3.4", "bytes_in": 100, "bytes_out": 0},
            {"proto": "tcp4", "local": "10.0.0.2:5001", "remote": "1.2.3.4:443",
             "host": "1.2.3.4", "bytes_in": 700, "bytes_out": 0}]}]
        netpeer.record(self.conn, rows, self.now)
        rows[0]["conns"][0]["bytes_in"] = 150
        rows[0]["conns"][1]["bytes_in"] = 900
        netpeer.record(self.conn, rows, self.now + 30)
        # 50 from one socket and 200 from the other, folded into one peer row.
        self.assertEqual(self.rows(),
                         [(netpeer.bucket_of(self.now), "1.2.3.4", "Arc",
                           250, 0)])

    def test_wildcard_peers_are_skipped(self):
        netpeer.record(self.conn, [socket_row("launchd", "*:*", "*", 0, 0)],
                       self.now)
        netpeer.record(self.conn, [socket_row("launchd", "*:*", "*", 500, 0)],
                       self.now + 30)
        self.assertEqual(self.rows(), [])

    def test_separate_apps_to_one_host_stay_separate(self):
        for app in ("Arc", "cloudd"):
            netpeer.record(self.conn, [socket_row(app, "1.2.3.4:443", "1.2.3.4",
                                                  100, 0)], self.now)
            netpeer.record(self.conn, [socket_row(app, "1.2.3.4:443", "1.2.3.4",
                                                  400, 0)], self.now + 30)
        self.assertEqual([r[2] for r in self.rows()], ["Arc", "cloudd"])

    def test_prune_drops_history_past_the_window(self):
        old = self.now - netpeer.KEEP - netpeer.BUCKET
        self.conn.execute("INSERT INTO net_peer VALUES (?,?,?,?,?)",
                          (netpeer.bucket_of(old), "1.2.3.4", "Arc", 1, 1))
        self.conn.execute("INSERT INTO net_peer VALUES (?,?,?,?,?)",
                          (netpeer.bucket_of(self.now), "5.6.7.8", "Arc", 1, 1))
        netpeer.prune(self.conn, self.now)
        self.assertEqual([r[1] for r in self.rows()], ["5.6.7.8"])

    def test_prune_drops_counters_for_dead_sockets(self):
        netpeer.record(self.conn, [socket_row("Arc", "1.2.3.4:443", "1.2.3.4",
                                              100, 0)], self.now)
        netpeer.prune(self.conn, self.now + netpeer.STATE_TTL + 60)
        left = self.conn.execute(
            "SELECT COUNT(*) FROM net_peer_state").fetchone()[0]
        self.assertEqual(left, 0)

    def test_peers_reports_a_window_busiest_first(self):
        slot = netpeer.bucket_of(self.now)
        self.conn.executemany(
            "INSERT INTO net_peer VALUES (?,?,?,?,?)",
            [(slot, "1.1.1.1", "Arc", 10, 0),
             (slot, "2.2.2.2", "cloudd", 900, 100)])
        self.conn.commit()
        found = netpeer.peers(self.conn, slot, slot + netpeer.BUCKET)
        self.assertEqual([p["ip"] for p in found], ["2.2.2.2", "1.1.1.1"])
        self.assertEqual(found[0]["bytes_in"], 900)

    def test_peers_outside_the_window_are_not_reported(self):
        slot = netpeer.bucket_of(self.now)
        self.conn.execute("INSERT INTO net_peer VALUES (?,?,?,?,?)",
                          (slot, "1.1.1.1", "Arc", 10, 0))
        self.conn.commit()
        self.assertEqual(netpeer.peers(self.conn, slot + netpeer.BUCKET,
                                       slot + 2 * netpeer.BUCKET), [])

    def test_system_applications_are_flagged_from_interned_identities(self):
        """So the history obeys the same "hide system processes" switch the
        live panel does."""
        slot = netpeer.bucket_of(self.now)
        self.conn.executemany(
            "INSERT INTO proc (exe, args_sig, cmdline_full, is_system, app) "
            "VALUES (?,?,?,?,?)",
            [("cloudd", "", "cloudd", 1, "cloudd"),
             ("Arc", "", "Arc", 0, "Arc")])
        self.conn.executemany("INSERT INTO net_peer VALUES (?,?,?,?,?)",
                              [(slot, "1.1.1.1", "cloudd", 10, 0),
                               (slot, "2.2.2.2", "Arc", 20, 0)])
        self.conn.commit()
        found = {p["app"]: p["is_system"]
                 for p in netpeer.peers(self.conn, slot, slot + netpeer.BUCKET)}
        self.assertTrue(found["cloudd"])
        self.assertFalse(found["Arc"])

    def test_an_unknown_application_is_not_assumed_to_be_macos(self):
        """Hiding system processes must not hide something merely unseen."""
        slot = netpeer.bucket_of(self.now)
        self.conn.execute("INSERT INTO net_peer VALUES (?,?,?,?,?)",
                          (slot, "1.1.1.1", "SomethingNew", 10, 0))
        self.conn.commit()
        found = netpeer.peers(self.conn, slot, slot + netpeer.BUCKET)
        self.assertFalse(found[0]["is_system"])

    def test_private_addresses_are_flagged(self):
        slot = netpeer.bucket_of(self.now)
        self.conn.executemany("INSERT INTO net_peer VALUES (?,?,?,?,?)",
                              [(slot, "192.168.1.5", "Arc", 10, 0),
                               (slot, "17.253.144.10", "apsd", 20, 0)])
        self.conn.commit()
        found = {p["ip"]: p["private"]
                 for p in netpeer.peers(self.conn, slot, slot + netpeer.BUCKET)}
        self.assertTrue(found["192.168.1.5"])
        self.assertFalse(found["17.253.144.10"])

    def test_span_reports_what_is_held(self):
        self.assertIsNone(netpeer.span(self.conn))
        slot = netpeer.bucket_of(self.now)
        self.conn.execute("INSERT INTO net_peer VALUES (?,?,?,?,?)",
                          (slot, "1.1.1.1", "Arc", 1, 1))
        self.conn.commit()
        self.assertEqual(netpeer.span(self.conn)["first"], slot)

    def test_timeline_buckets_activity_across_the_window(self):
        slot = netpeer.bucket_of(self.now)
        self.conn.executemany(
            "INSERT INTO net_peer VALUES (?,?,?,?,?)",
            [(slot, "1.1.1.1", "Arc", 100, 0),
             (slot, "2.2.2.2", "Arc", 100, 0),
             (slot + netpeer.BUCKET, "1.1.1.1", "Arc", 50, 0)])
        self.conn.commit()
        track = netpeer.timeline(self.conn, slot, slot + 2 * netpeer.BUCKET,
                                 slots=2)
        self.assertEqual(track["points"][0]["bytes"], 200)
        self.assertEqual(track["points"][0]["peers"], 2)

    def test_schema_is_registered_for_every_database(self):
        """netpeer.DDL has to be reachable from db.init_schema, or the sampler
        writes to a table that only exists in this test."""
        fresh = db.connect(":memory:")
        db.init_schema(fresh)
        found = fresh.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('net_peer','net_peer_state')").fetchall()
        self.assertEqual(len(found), 2)
        fresh.close()


if __name__ == "__main__":
    unittest.main()
