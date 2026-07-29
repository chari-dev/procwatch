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


if __name__ == "__main__":
    unittest.main()
