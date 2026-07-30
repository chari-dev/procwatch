import os
import shutil
import tempfile
import time
import unittest

from procwatch import config, db, share


class TestKey(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.real_db = config.DB_PATH
        config.DB_PATH = os.path.join(self.dir, "test.db")
        self.conn = db.connect(config.DB_PATH)
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self.real_db
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_key_is_three_words(self):
        self.assertEqual(len(share.make_key().split("-")), 3)

    def test_the_words_are_ones_a_person_can_read_aloud(self):
        """The system dictionary would give more bits and words like
        "exaudi" and "peepy". A key that cannot be dictated down a phone gets
        written on a sticky note instead."""
        pool = set(share._wordlist())
        for _ in range(20):
            for word in share.make_key().split("-"):
                self.assertIn(word, pool)
                self.assertTrue(word.isalpha() and word.islower())
                self.assertLessEqual(len(word), 6)

    def test_two_keys_differ(self):
        self.assertNotEqual(share.make_key(), share.make_key())

    def test_the_key_is_kept_once_made(self):
        # It is read off another machine's screen and typed in here; changing
        # on every start would break every device that had been set up.
        first = share.key(self.conn)
        self.assertEqual(share.key(self.conn), first)

    def test_a_new_key_can_be_demanded(self):
        first = share.key(self.conn)
        self.assertNotEqual(share.key(self.conn, reset=True), first)

    def test_the_pool_is_large_enough_to_matter(self):
        # Alongside the lockout, five guesses a minute against this many
        # combinations is not a practical attack.
        self.assertGreaterEqual(len(share._wordlist()), 400)
        self.assertEqual(len(set(share._wordlist())), len(share._wordlist()))


class TestLockout(unittest.TestCase):
    def setUp(self):
        share._failures.clear()

    def tearDown(self):
        share._failures.clear()

    def test_a_few_wrong_answers_are_forgiven(self):
        # Someone mistyping their own key should not be punished for a minute.
        for _ in range(share.LOCKOUT_AFTER - 1):
            share._note_failure("10.0.0.9")
        self.assertFalse(share._locked_out("10.0.0.9"))

    def test_persistent_guessing_is_shut_out(self):
        for _ in range(share.LOCKOUT_AFTER):
            share._note_failure("10.0.0.9")
        self.assertTrue(share._locked_out("10.0.0.9"))

    def test_the_lockout_is_per_address(self):
        for _ in range(share.LOCKOUT_AFTER):
            share._note_failure("10.0.0.9")
        self.assertFalse(share._locked_out("10.0.0.10"))

    def test_a_correct_key_clears_the_count(self):
        for _ in range(share.LOCKOUT_AFTER - 1):
            share._note_failure("10.0.0.9")
        share._note_success("10.0.0.9")
        for _ in range(share.LOCKOUT_AFTER - 1):
            share._note_failure("10.0.0.9")
        self.assertFalse(share._locked_out("10.0.0.9"))


class TestReadOnly(unittest.TestCase):
    """The listener's safety is structural, not a setting.

    The dashboard can end processes and hand out the database. This port
    cannot, because those routes are not present on it -- which is a different
    and much stronger claim than their being switched off.
    """

    def test_there_is_no_way_to_post_to_it(self):
        self.assertFalse(hasattr(share.ShareHandler, "do_POST"))
        self.assertFalse(hasattr(share.ShareHandler, "do_PUT"))
        self.assertFalse(hasattr(share.ShareHandler, "do_DELETE"))

    def test_the_shared_api_has_no_route_to_anything_dangerous(self):
        """Asserted against the dispatcher rather than the source text.

        Everything this port can answer goes through server.api_get, and that
        function knows nothing of killing, backing up or exporting -- those
        live in the local HTTP handler. So they are unreachable here by
        construction, and this test fails the moment someone moves one in.
        """
        import os as _os, shutil as _shutil, tempfile as _tempfile
        from procwatch import db as _db, server
        folder = _tempfile.mkdtemp()
        real = config.DB_PATH
        config.DB_PATH = _os.path.join(folder, "t.db")
        conn = _db.connect(config.DB_PATH)
        _db.init_schema(conn)
        try:
            for path in ("/api/kill", "/api/backup", "/api/export",
                         "/api/remote", "/api/peers", "/", "/index.html"):
                self.assertIsNone(server.api_get(conn, path, {}),
                                  "%s is reachable through the shared port" % path)
            # And the reads it is for do work.
            self.assertIsNotNone(server.api_get(conn, "/api/info", {}))
        finally:
            conn.close()
            config.DB_PATH = real
            _shutil.rmtree(folder, ignore_errors=True)

    def test_the_key_is_compared_without_leaking_its_length(self):
        import inspect
        self.assertIn("compare_digest", inspect.getsource(share.ShareHandler))


class TestCorsPolicy(unittest.TestCase):
    """Who may read this server's answers from another origin.

    Lived in test_pages.py, which covered the hosted viewer page. That page is
    gone; this is not about it -- a page on any site can send a request to a
    loopback address and have it delivered, and what the allowlist decides is
    whether that page may read the reply.
    """

    def test_it_is_never_a_wildcard(self):
        self.assertNotIn("*", share.ALLOWED_ORIGINS)

    def test_it_lists_only_local_development_origins(self):
        # The project's own site was on this list so a page hosted there could
        # act as a viewer. That page is gone, and an origin left behind after
        # its reason has gone is a door nobody is watching.
        for origin in share.ALLOWED_ORIGINS:
            self.assertTrue(
                origin.startswith("http://localhost")
                or origin.startswith("http://127.0.0.1"),
                "%s is not a local origin" % origin)

    def test_the_reply_varies_by_origin(self):
        # Without it a shared cache could hand one origin's permission to
        # another.
        import inspect
        self.assertIn('"Vary", "Origin"',
                      inspect.getsource(share.ShareHandler._cors))


if __name__ == "__main__":
    unittest.main()


class TestPhoneAccess(unittest.TestCase):
    """Serving the dashboard to a phone on the same network.

    A hosted page cannot do this job. A browser refuses to let an HTTPS page
    read a plain-HTTP private address at all -- it is a Mixed Content block,
    not something a header can permit -- so the page has to come from the Mac
    that has the data. localhost is exempt from that rule, which is why tools
    that only ever talk to 127.0.0.1 can be hosted elsewhere; a phone cannot
    use that exemption.
    """

    def _offered(self, headers=None, params=None):
        """Run the real key-extraction against stub request state.

        Asserted by calling it rather than by reading its source: what matters
        is which of the three ways a client can present a key actually work.
        """
        stub = share.ShareHandler.__new__(share.ShareHandler)
        stub.headers = headers or {}
        return share.ShareHandler._offered_key(stub, params or {})

    def test_a_relay_can_use_a_header(self):
        self.assertEqual(self._offered({share.HEADER: "a-b-c"}),
                         ("a-b-c", "header"))

    def test_a_phone_can_use_the_query_string(self):
        # A phone cannot add a request header by typing a URL.
        self.assertEqual(self._offered(params={"key": ["a-b-c"]}),
                         ("a-b-c", "query"))

    def test_a_browser_can_use_the_cookie_it_was_given(self):
        self.assertEqual(
            self._offered({"Cookie": "other=1; %s=a-b-c" % share.COOKIE}),
            ("a-b-c", "cookie"))

    def test_nothing_offered_is_reported_as_nothing(self):
        # Distinct from a wrong key: a first visit must not count as a failed
        # attempt towards the lockout.
        self.assertEqual(self._offered(), ("", "none"))

    def test_the_key_page_asks_for_three_words(self):
        self.assertIn("three words", share.KEY_PAGE)
        self.assertIn('method="get"', share.KEY_PAGE)

    def test_the_key_page_scales_to_a_phone(self):
        self.assertIn("width=device-width", share.KEY_PAGE)

    def test_the_key_page_escapes_what_it_interpolates(self):
        # Both placeholders carry runtime values into HTML.
        self.assertIn("__HOST__", share.KEY_PAGE)
        self.assertIn("__ERROR__", share.KEY_PAGE)
        self.assertEqual(share._escape('<img src=x onerror="a">'),
                         "&lt;img src=x onerror=&quot;a&quot;&gt;")

    def test_serving_a_page_is_still_not_serving_a_way_to_act(self):
        # The dashboard is sent, but nothing behind its buttons exists here.
        self.assertFalse(hasattr(share.ShareHandler, "do_POST"))

    def test_the_dashboard_is_marked_read_only(self):
        import inspect
        source = inspect.getsource(share.ShareHandler._send_dashboard)
        self.assertIn("procwatch-readonly", source)

    def test_both_servers_read_the_same_dashboard(self):
        # A second copy of the page for the shared port would drift out of
        # step with the local one.
        from procwatch import server
        self.assertTrue(callable(getattr(server, "dashboard_html", None)))
        import inspect
        self.assertIn("dashboard_html",
                      inspect.getsource(share.ShareHandler._send_dashboard))
