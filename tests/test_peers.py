import json
import os
import shutil
import subprocess
import tempfile
import unittest

from procwatch import config, peers


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


class TestRegistry(PeerCase):
    def test_a_peer_round_trips(self):
        peers.add("laptop", "me@host")
        self.assertEqual(peers.listing(),
                         [{"name": "laptop", "host": "me@host", "program": ""}])

    def test_adding_the_same_name_replaces_rather_than_duplicates(self):
        peers.add("laptop", "me@old")
        peers.add("laptop", "me@new")
        self.assertEqual([p["host"] for p in peers.listing()], ["me@new"])

    def test_a_peer_needs_a_host(self):
        with self.assertRaises(ValueError):
            peers.add("laptop", "")

    def test_the_local_machine_s_name_is_reserved(self):
        # The switcher always offers "This Mac"; a peer by that name would be
        # two different machines under one label.
        with self.assertRaises(ValueError):
            peers.add("This Mac", "me@host")

    def test_removing_reports_whether_it_existed(self):
        peers.add("laptop", "me@host")
        self.assertTrue(peers.remove("laptop"))
        self.assertFalse(peers.remove("laptop"))


class TestFetch(PeerCase):
    """What actually reaches the far side.

    Everything here runs over ssh, so the command is assembled rather than
    typed -- and a peer's name arrives from a browser.
    """

    def setUp(self):
        super(TestFetch, self).setUp()
        peers.add("laptop", "me@host")
        self.calls = []
        self.real_run = subprocess.run

        def fake_run(cmd, **kw):
            self.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"ok": 1}), "")

        subprocess.run = fake_run

    def tearDown(self):
        subprocess.run = self.real_run
        super(TestFetch, self).tearDown()

    def test_it_runs_fetch_on_the_peer(self):
        self.assertEqual(peers.fetch("laptop", "/api/info", {}), {"ok": 1})
        command = self.calls[0]
        self.assertEqual(command[0], "ssh")
        self.assertIn("me@host", command)
        self.assertIn("fetch", command[-1])

    def test_it_refuses_to_hang_on_a_sleeping_machine(self):
        # A laptop with the lid shut must fail quickly, not block the page.
        command = self.calls if self.calls else peers.fetch("laptop", "/api/info", {}) or self.calls
        peers.fetch("laptop", "/api/info", {})
        flat = " ".join(self.calls[-1])
        self.assertIn("ConnectTimeout", flat)
        self.assertIn("BatchMode=yes", flat)

    def test_a_path_with_shell_characters_stays_one_argument(self):
        """The remote string is parsed by a shell on the far side.

        Asserted by parsing it the way that shell would: the dangerous text
        has to come out as a single argument to fetch, not as a second
        command. Checking for quotes in the string proves nothing -- what
        matters is how it parses.
        """
        import shlex
        peers.fetch("laptop", "/api/info; rm -rf ~", {})
        argv = shlex.split(self.calls[-1][-1])
        self.assertEqual(argv[0], "python3")
        self.assertEqual(argv[2], "fetch")
        self.assertEqual(argv[3], "/api/info; rm -rf ~")
        self.assertNotIn("rm", argv[:3])

    def test_query_values_cannot_become_shell_syntax(self):
        # They are URL-encoded before they are ever quoted, so a semicolon
        # arrives as %3B and could not act as one even unquoted.
        import shlex
        peers.fetch("laptop", "/api/series", {"scope": ["apps; whoami"]})
        argv = shlex.split(self.calls[-1][-1])
        self.assertEqual(len(argv), 5)
        self.assertNotIn(";", argv[4])
        self.assertIn("%3B", argv[4])

    def test_an_unknown_peer_is_a_key_error(self):
        with self.assertRaises(KeyError):
            peers.fetch("nope", "/api/info", {})


class TestCheck(PeerCase):
    def setUp(self):
        super(TestCheck, self).setUp()
        peers.add("laptop", "me@host")
        self.real_fetch = peers.fetch

    def tearDown(self):
        peers.fetch = self.real_fetch
        super(TestCheck, self).tearDown()

    def test_age_is_measured_against_the_peers_own_clock(self):
        """Two Macs can disagree by hours, and these two do.

        Measuring a remote sample's age with the local clock reports a
        perfectly healthy recorder as hours stale.
        """
        peers.fetch = lambda *a, **k: {"hostname": "far", "now": 1_000_000,
                                       "last_tick": 999_970}
        state = peers.check("laptop")
        self.assertTrue(state["ok"])
        self.assertEqual(state["last_tick_age"], 30)

    def test_an_unreachable_peer_reports_rather_than_raises(self):
        def boom(*a, **k):
            raise RuntimeError("ssh: connect to host me@host port 22: timed out")
        peers.fetch = boom
        state = peers.check("laptop")
        self.assertFalse(state["ok"])
        self.assertIn("timed out", state["error"])


if __name__ == "__main__":
    unittest.main()
