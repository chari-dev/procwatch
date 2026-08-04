"""The two settings the recorder has to be able to read.

A tiny store on purpose. What it has to guarantee is that nothing in it can make
a sampler tick fail: a hand-edited row, a stale value from an older build, or a
request from the dashboard carrying anything at all.
"""
import unittest

from procwatch import db, prefs


def _conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


class TestDefaults(unittest.TestCase):
    def test_a_fresh_database_has_the_defaults(self):
        self.assertEqual(prefs.all_prefs(_conn()),
                         {"findings_enabled": "1", "findings_notify": "causes",
                          "geo_lookup": "1"})

    def test_findings_are_on_to_begin_with(self):
        # The feature is the point of the program; it is opt-out, not opt-in.
        self.assertTrue(prefs.findings_on(_conn()))

    def test_notifications_default_to_the_quieter_of_the_two(self):
        # Every finding would include the merely costly, which is a lot of
        # notifications for a machine doing ordinary housekeeping.
        self.assertEqual(prefs.get(_conn(), "findings_notify"), "causes")


class TestValidation(unittest.TestCase):
    def test_a_hand_edited_value_falls_back_rather_than_propagating(self):
        # The alternative is a typo in the database deciding what the recorder
        # does.
        conn = _conn()
        with conn:
            conn.execute("INSERT OR REPLACE INTO pref (key, value, set_ts) "
                         "VALUES ('findings_notify','shout',0)")
        self.assertEqual(prefs.get(conn, "findings_notify"), "causes")

    def test_an_unknown_value_is_refused_on_the_way_in(self):
        with self.assertRaises(ValueError):
            prefs.set(_conn(), "findings_notify", "shout")

    def test_an_unknown_key_is_refused(self):
        # So a request from the dashboard cannot invent settings.
        with self.assertRaises(KeyError):
            prefs.set(_conn(), "wibble", "1")
        with self.assertRaises(KeyError):
            prefs.get(_conn(), "wibble")

    def test_booleans_from_json_are_understood(self):
        # The dashboard sends what a select gives it, and a checkbox elsewhere
        # would send true. Both mean the same thing.
        conn = _conn()
        self.assertEqual(prefs.set(conn, "findings_enabled", True), "1")
        self.assertEqual(prefs.set(conn, "findings_enabled", False), "0")
        self.assertFalse(prefs.findings_on(conn))

    def test_a_database_without_the_table_still_answers(self):
        # An older database restored from a backup has no pref table. A missing
        # setting has to cost its default, not the tick.
        conn = _conn()
        with conn:
            conn.execute("DROP TABLE pref")
        self.assertEqual(prefs.get(conn, "findings_notify"), "causes")
        self.assertTrue(prefs.findings_on(conn))


class TestRoundTrip(unittest.TestCase):
    def test_what_is_set_is_what_comes_back(self):
        conn = _conn()
        for value in prefs.NOTIFY_CHOICES:
            prefs.set(conn, "findings_notify", value)
            self.assertEqual(prefs.get(conn, "findings_notify"), value)

    def test_setting_twice_leaves_one_row(self):
        conn = _conn()
        prefs.set(conn, "findings_notify", "all")
        prefs.set(conn, "findings_notify", "off")
        count = conn.execute("SELECT COUNT(*) FROM pref WHERE "
                             "key='findings_notify'").fetchone()[0]
        self.assertEqual(count, 1)


class TestEndpoint(unittest.TestCase):
    def test_the_api_reports_them(self):
        from procwatch import server
        out = server.api_get(_conn(), "/api/prefs", {})
        self.assertIn("findings_enabled", out)
        self.assertIn("findings_notify", out)


if __name__ == "__main__":
    unittest.main()
