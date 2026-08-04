"""The live network view: nettop's connection rows, rates, and grouping."""
import unittest
from unittest import mock

from procwatch import live, netstat

SAMPLE = """,bytes_in,bytes_out,
launchd.1,0,0,
tcp4 127.0.0.1:8021<->*:*,,,
apsd.369,9215754,30461619,
tcp4 192.168.4.50:56611<->17.188.169.70:5223,9215754,30461619,
airportd.508,0,0,
udp4 *:*<->*:*,,,
mDNSResponder.515,34688165,12814935,
tcp4 192.168.4.50:52631<->8.8.8.8:853,147190,66270,
udp6 *.5353<->*.*,16553148,6081404,
Arc Helper.777,500,600,
tcp6 fe80::1.52000<->2606:4700::6810:84e5.443,500,600,
"""


class TestParseTraffic(unittest.TestCase):
    def test_processes_carry_their_connections(self):
        rows = netstat.parse_traffic(SAMPLE)
        by_pid = {r["pid"]: r for r in rows}
        self.assertEqual(by_pid[369]["bytes_in"], 9215754)
        self.assertEqual(len(by_pid[369]["conns"]), 1)
        conn = by_pid[369]["conns"][0]
        self.assertEqual(conn["host"], "17.188.169.70")
        self.assertEqual(conn["port"], "5223")
        self.assertEqual(conn["bytes_out"], 30461619)

    def test_listeners_and_unconnected_sockets_are_not_traffic(self):
        rows = netstat.parse_traffic(SAMPLE)
        by_pid = {r["pid"]: r for r in rows}
        self.assertEqual(by_pid[1]["conns"], [])      # <->*:* listener
        self.assertEqual(by_pid[508]["conns"], [])    # wildcard sockets
        # The udp6 wildcard row under mDNSResponder is dropped; the DNS-over-
        # TLS connection stays.
        self.assertEqual([c["host"] for c in by_pid[515]["conns"]],
                         ["8.8.8.8"])

    def test_a_name_with_spaces_and_a_v6_peer(self):
        rows = netstat.parse_traffic(SAMPLE)
        arc = [r for r in rows if r["name"] == "Arc Helper"][0]
        self.assertEqual(arc["pid"], 777)
        conn = arc["conns"][0]
        self.assertEqual(conn["host"], "2606:4700::6810:84e5")
        self.assertEqual(conn["port"], "443")

    def test_garbage_parses_to_nothing(self):
        self.assertEqual(netstat.parse_traffic(""), [])
        self.assertEqual(netstat.parse_traffic("no header here\n"), [])


class TestSplitEndpoint(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(netstat.split_endpoint("1.2.3.4:443"),
                         ("1.2.3.4", "443"))
        self.assertEqual(netstat.split_endpoint("::1.8021"), ("::1", "8021"))
        self.assertEqual(netstat.split_endpoint("*:*"), ("*:*", ""))


class TestRates(unittest.TestCase):
    def test_rates_are_deltas_over_time(self):
        prev = {5: (100, 200)}
        cur = {5: (300, 200)}
        self.assertEqual(live.rate_deltas(prev, cur, 10.0), {5: (20.0, 0.0)})

    def test_a_counter_that_went_backwards_is_a_different_process(self):
        self.assertEqual(live.rate_deltas({5: (100, 0)}, {5: (50, 0)}, 10.0),
                         {})

    def test_no_baseline_no_rates(self):
        self.assertEqual(live.rate_deltas(None, {5: (1, 1)}, 10.0), {})
        self.assertEqual(live.rate_deltas({}, {5: (1, 1)}, 0), {})


class TestNetworkTraffic(unittest.TestCase):
    def test_grouped_by_application_with_rates(self):
        traffic = [{"pid": 10, "name": "Arc Helper", "bytes_in": 100,
                    "bytes_out": 50,
                    "conns": [{"proto": "tcp4", "local": "l", "remote": "r",
                               "host": "1.2.3.4", "port": "443",
                               "bytes_in": 100, "bytes_out": 50}]},
                   {"pid": 11, "name": "Arc", "bytes_in": 10, "bytes_out": 5,
                    "conns": []}]
        proc = mock.Mock()
        with mock.patch.dict(live._net, {"traffic": traffic,
                                         "rates": {10: (7.0, 3.0)},
                                         "ts": 1000.0}), \
                mock.patch.object(live.psreader, "read", return_value=[]), \
                mock.patch.object(live, "peer_name", return_value="cdn.example"):
            del proc  # identity comes from nettop's names when ps knows nothing
            out = live.network_traffic()
        apps = {a["app"]: a for a in out["apps"]}
        self.assertIn("Arc Helper", apps)
        self.assertEqual(apps["Arc Helper"]["in_rate"], 7.0)
        self.assertEqual(apps["Arc Helper"]["conns"][0]["peer"], "cdn.example")
        self.assertEqual(out["total_in"], 7.0)


if __name__ == "__main__":
    unittest.main()
