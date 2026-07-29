import sqlite3, unittest
from procwatch import db, config


class TestSchema(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)

    def _tables(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {r[0] for r in rows}

    def test_every_tier_has_a_sample_and_system_table(self):
        names = self._tables()
        for tier in config.TIERS:
            self.assertIn("sample_" + tier.name, names)
            self.assertIn("system_" + tier.name, names)

    def test_supporting_tables_exist(self):
        self.assertLessEqual(
            {"proc", "watchlist", "gap", "sampler_state"}, self._tables())

    def test_identity_is_unique_on_exe_and_args(self):
        self.conn.execute(
            "INSERT INTO proc (exe, args_sig, cmdline_full) VALUES (?,?,?)",
            ("log", "stream --predicate X", "/usr/bin/log stream --predicate X"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO proc (exe, args_sig, cmdline_full) VALUES (?,?,?)",
                ("log", "stream --predicate X", "different full text"))

    def test_init_schema_is_idempotent(self):
        db.init_schema(self.conn)   # must not raise on a populated database
        self.assertIn("proc", self._tables())

    def test_tiers_are_ordered_coarsening_and_divide_evenly(self):
        for finer, coarser in zip(config.TIERS, config.TIERS[1:]):
            self.assertLess(finer.seconds, coarser.seconds)
            self.assertEqual(coarser.seconds % finer.seconds, 0)

    def test_only_the_last_tier_is_retained_forever(self):
        self.assertIsNone(config.TIERS[-1].retain_seconds)
        for tier in config.TIERS[:-1]:
            self.assertIsNotNone(tier.retain_seconds)


if __name__ == "__main__":
    unittest.main()
