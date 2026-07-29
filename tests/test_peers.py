import json
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request

from procwatch import config, peers, share


class PeerCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.real_db = config.DB_PATH
        config.DB_PATH = os.path.join(self.dir, "test.db")
        from procwatch import db
        conn = db.connect(config.DB_PATH)
        db.init_schema(conn)
        conn.close()

    def tearDown(self):
        config.DB_PATH = self.real_db
        shutil.rmtree(self.dir, ignore_errors=True)


class TestAddresses(PeerCase):
    """What people type has to work.

    Being corrected about a URL scheme is a poor first experience of a feature
    whose entire pitch is that it is simple.
    """

    def test_a_bare_address_gets_a_scheme_and_the_default_port(self):
        self.assertEqual(peers.normalise("192.168.1.42"),
                         "http://192.168.1.42:%d" % share.DEFAULT_PORT)

    def test_an_address_with_a_port_is_left_alone(self):
        self.assertEqual(peers.normalise("192.168.1.42:9000"),
                         "http://192.168.1.42:9000")

    def test_a_full_url_is_accepted(self):
        self.assertEqual(peers.normalise("http://laptop.local:8791/"),
                         "http://laptop.local:8791")

    def test_a_hostname_works_like_an_address(self):
        self.assertEqual(peers.normalise("laptop.local"),
                         "http://laptop.local:%d" % share.DEFAULT_PORT)

    def test_an_empty_address_is_refused(self):
        with self.assertRaises(ValueError):
            peers.normalise("")


class TestRegistry(PeerCase):
    def test_a_device_round_trips(self):
        peers.add("laptop", "192.168.1.42", "fill-root-odds")
        got = peers.listing(with_keys=True)[0]
        self.assertEqual(got["name"], "laptop")
        self.assertEqual(got["host"], "http://192.168.1.42:8791")
        self.assertEqual(got["key"], "fill-root-odds")

    def test_the_key_is_not_handed_to_the_browser(self):
        """The page has no use for it.

        A dashboard left open on a shared screen should not be a list of the
        keys to every other machine you own.
        """
        peers.add("laptop", "192.168.1.42", "fill-root-odds")
        self.assertNotIn("key", peers.listing()[0])

    def test_adding_the_same_name_replaces_rather_than_duplicates(self):
        peers.add("laptop", "10.0.0.1", "a-b-c")
        peers.add("laptop", "10.0.0.2", "d-e-f")
        rows = peers.listing(with_keys=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "d-e-f")

    def test_the_local_machines_name_is_reserved(self):
        with self.assertRaises(ValueError):
            peers.add("This Mac", "10.0.0.1", "a-b-c")

    def test_removing_reports_whether_it_existed(self):
        peers.add("laptop", "10.0.0.1", "a-b-c")
        self.assertTrue(peers.remove("laptop"))
        self.assertFalse(peers.remove("laptop"))


class TestFetch(PeerCase):
    def setUp(self):
        super(TestFetch, self).setUp()
        peers.add("laptop", "10.0.0.1", "fill-root-odds")
        self.seen = {}
        self.real_open = urllib.request.urlopen

    def tearDown(self):
        urllib.request.urlopen = self.real_open
        super(TestFetch, self).tearDown()

    def _answer(self, payload=None, error=None):
        def fake(request, timeout=None):
            self.seen["url"] = request.full_url
            self.seen["headers"] = dict(request.headers)
            self.seen["timeout"] = timeout
            if error:
                raise error
            class Response(object):
                def read(self):
                    return json.dumps(payload).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
            return Response()
        urllib.request.urlopen = fake

    def test_it_asks_the_right_url_with_the_key(self):
        self._answer({"ok": 1})
        self.assertEqual(peers.fetch("laptop", "/api/info", {}), {"ok": 1})
        self.assertEqual(self.seen["url"], "http://10.0.0.1:8791/api/info")
        self.assertIn("fill-root-odds", str(self.seen["headers"]))

    def test_query_parameters_are_encoded(self):
        self._answer({})
        peers.fetch("laptop", "/api/series", {"scope": ["apps"], "limit": ["12"]})
        self.assertIn("scope=apps", self.seen["url"])
        self.assertIn("limit=12", self.seen["url"])

    def test_it_does_not_wait_forever_on_a_sleeping_machine(self):
        self._answer({})
        peers.fetch("laptop", "/api/info", {})
        self.assertTrue(0 < self.seen["timeout"] <= 60)

    def test_a_refused_key_says_so_plainly(self):
        self._answer(error=urllib.error.HTTPError(
            "u", 401, "Unauthorized", {}, None))
        with self.assertRaises(RuntimeError) as caught:
            peers.fetch("laptop", "/api/info", {})
        self.assertIn("refused the key", str(caught.exception))

    def test_being_locked_out_says_so_plainly(self):
        self._answer(error=urllib.error.HTTPError("u", 429, "Too Many", {}, None))
        with self.assertRaises(RuntimeError) as caught:
            peers.fetch("laptop", "/api/info", {})
        self.assertIn("refusing keys", str(caught.exception))

    def test_an_unreachable_machine_suggests_the_likely_cause(self):
        self._answer(error=urllib.error.URLError("Connection refused"))
        with self.assertRaises(RuntimeError) as caught:
            peers.fetch("laptop", "/api/info", {})
        self.assertIn("procwatch share", str(caught.exception))

    def test_an_unknown_device_is_a_key_error(self):
        with self.assertRaises(KeyError):
            peers.fetch("nope", "/api/info", {})


class TestCheck(PeerCase):
    def setUp(self):
        super(TestCheck, self).setUp()
        peers.add("laptop", "10.0.0.1", "a-b-c")
        self.real_fetch = peers.fetch

    def tearDown(self):
        peers.fetch = self.real_fetch
        super(TestCheck, self).tearDown()

    def test_age_is_measured_against_that_machines_clock(self):
        # Two Macs can be hours apart in absolute time. Measuring with the
        # local clock reports a healthy recorder as long stale.
        peers.fetch = lambda *a, **k: {"hostname": "far", "now": 1_000_000,
                                       "last_tick": 999_970}
        self.assertEqual(peers.check("laptop")["last_tick_age"], 30)

    def test_an_unreachable_device_reports_rather_than_raises(self):
        def boom(*a, **k):
            raise RuntimeError("cannot reach laptop")
        peers.fetch = boom
        state = peers.check("laptop")
        self.assertFalse(state["ok"])
        self.assertIn("cannot reach", state["error"])


if __name__ == "__main__":
    unittest.main()
