"""Pruning the tables that are not samples: app versions and power events.

These ran against config.TIERS through an attribute the tier does not have,
so every call raised AttributeError. Both callers in main.py catch broadly
and log, so nothing crashed and nothing worked -- the only symptom was a
line in a log, ten thousand times over. Every test here calls the real
function against the real tier table, which is the part that was missing.
"""
import time
import unittest

from procwatch import config, db, power, versions


class TestTiers(unittest.TestCase):
    def test_the_tier_carries_the_field_the_pruners_ask_for(self):
        self.assertIn("retain_seconds", config.Tier._fields)
        self.assertNotIn("keep", config.Tier._fields)

    def test_at_least_one_tier_is_kept_forever(self):
        # Which is why the pruners filter None out before taking a maximum.
        self.assertTrue(any(t.retain_seconds is None for t in config.TIERS))
        self.assertTrue(any(t.retain_seconds for t in config.TIERS))


class TestVersionPrune(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)
        self.now = int(time.time())
        self.addCleanup(self.conn.close)
        versions.init(self.conn)

    def _add(self, app, last_ts):
        with self.conn:
            self.conn.execute(
                "INSERT INTO app_version (app, version, first_ts, last_ts) "
                "VALUES (?,?,?,?)", (app, "1.0", last_ts, last_ts))

    def test_it_runs_at_all(self):
        # The whole bug: this raised AttributeError every time it was called.
        versions.prune(self.conn, self.now)

    def test_it_keeps_what_is_recent(self):
        self._add("Kept", self.now - 86400)
        versions.prune(self.conn, self.now)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM app_version").fetchone()[0],
            1)

    def test_it_drops_what_is_older_than_the_longest_retention(self):
        longest = max(t.retain_seconds for t in config.TIERS
                      if t.retain_seconds)
        self._add("Ancient", self.now - longest - 86400)
        versions.prune(self.conn, self.now)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM app_version").fetchone()[0],
            0)


class TestPowerPrune(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)
        self.now = int(time.time())
        self.addCleanup(self.conn.close)
        power.init(self.conn)

    def test_it_runs_at_all(self):
        power.prune(self.conn, self.now)

    def test_it_keeps_recent_events_and_drops_ancient_ones(self):
        longest = max(t.retain_seconds for t in config.TIERS
                      if t.retain_seconds)
        with self.conn:
            for ts in (self.now - 3600, self.now - longest - 86400):
                self.conn.execute(
                    "INSERT INTO power_event (ts, kind, reason, charge) "
                    "VALUES (?,?,?,?)", (ts, "sleep", "test", 50))
        power.prune(self.conn, self.now)
        left = [r[0] for r in
                self.conn.execute("SELECT ts FROM power_event").fetchall()]
        self.assertEqual(left, [self.now - 3600])


class TestTheTickDoesNotSwallowThisSilently(unittest.TestCase):
    """main.py catches broadly around both, which is right -- a failed prune
    must not cost the sample. It is also what hid this for ten thousand
    ticks, so the pruners are exercised directly above rather than only
    through the tick."""

    def test_both_pruners_are_called_from_the_tick(self):
        import inspect
        from procwatch import main
        source = inspect.getsource(main.run_once)
        self.assertIn("versions.prune", source)
        self.assertIn("power.prune", source)


if __name__ == "__main__":
    unittest.main()
