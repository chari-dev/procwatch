import unittest

from procwatch import alerts, config, db


class TestRules(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)
        alerts.init(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_rule_round_trips(self):
        alerts.add(self.conn, "Arc", "cpu", 80, 600)
        got = alerts.rules(self.conn)[0]
        self.assertEqual((got["pattern"], got["metric"], got["threshold"],
                          got["sustain"]), ("Arc", "cpu", 80.0, 600))

    def test_an_unknown_metric_is_refused(self):
        with self.assertRaises(ValueError):
            alerts.add(self.conn, "Arc", "gpu", 80, 600)

    def test_a_sustain_shorter_than_a_sample_is_refused(self):
        # Nothing can be sustained for less time than the gap between
        # measurements; accepting it would promise something unmeasurable.
        with self.assertRaises(ValueError):
            alerts.add(self.conn, "Arc", "cpu", 80, config.INTERVAL - 1)

    def test_adding_the_same_rule_twice_keeps_one(self):
        alerts.add(self.conn, "Arc", "cpu", 80, 600)
        alerts.add(self.conn, "Arc", "cpu", 80, 600)
        self.assertEqual(len(alerts.rules(self.conn)), 1)

    def test_a_rule_can_be_removed(self):
        alerts.add(self.conn, "Arc", "cpu", 80, 600)
        rule_id = alerts.rules(self.conn)[0]["id"]
        self.assertTrue(alerts.remove(self.conn, rule_id))
        self.assertEqual(alerts.rules(self.conn), [])


class TestEvaluate(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)
        alerts.init(self.conn)
        self.now = 1_000_000
        self.arc = self._proc("Arc", "Arc")
        self.other = self._proc(config.OTHER, "")

    def tearDown(self):
        self.conn.close()

    def _proc(self, exe, app):
        return self.conn.execute(
            "INSERT INTO proc (exe, args_sig, cmdline_full, is_system, app) "
            "VALUES (?,'','',0,?)", (exe, app)).lastrowid

    def _samples(self, proc_id, cpu_percent, count, end=None):
        end = end or self.now
        for i in range(count):
            ts = end - i * config.INTERVAL
            self.conn.execute(
                "INSERT OR REPLACE INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, "
                "cpu_max_ts, rss_avg, rss_max, nproc, samples) "
                "VALUES (?,?,?,?,?,0,0,1,1)",
                (ts, proc_id, int(cpu_percent * 10), int(cpu_percent * 10), ts))
        self.conn.commit()

    def test_a_sustained_breach_fires(self):
        alerts.add(self.conn, "Arc", "cpu", 80, 300)
        self._samples(self.arc, 95, 11)
        raised = alerts.evaluate(self.conn, now=self.now)
        self.assertEqual(len(raised), 1)
        self.assertEqual(raised[0]["exe"], "Arc")

    def test_a_brief_spike_does_not(self):
        """The distinction the whole feature rests on.

        A compiler starting up hits 100% for one sample. Alerting on that is
        noise, and noise is what makes people turn alerts off.
        """
        alerts.add(self.conn, "Arc", "cpu", 80, 300)
        self._samples(self.arc, 5, 11)
        self._samples(self.arc, 99, 1)          # one sample, right at the end
        self.assertEqual(alerts.evaluate(self.conn, now=self.now), [])

    def test_too_few_samples_to_cover_the_window_does_not_fire(self):
        # Two high samples inside a ten-minute window is not ten minutes of
        # anything -- the machine may have been asleep for the rest of it.
        alerts.add(self.conn, "Arc", "cpu", 80, 600)
        self._samples(self.arc, 99, 2)
        self.assertEqual(alerts.evaluate(self.conn, now=self.now), [])

    def test_below_the_threshold_does_not_fire(self):
        alerts.add(self.conn, "Arc", "cpu", 80, 300)
        self._samples(self.arc, 79, 11)
        self.assertEqual(alerts.evaluate(self.conn, now=self.now), [])

    def test_a_star_pattern_matches_anything(self):
        alerts.add(self.conn, "*", "cpu", 80, 300)
        self._samples(self.arc, 95, 11)
        self.assertEqual(len(alerts.evaluate(self.conn, now=self.now)), 1)

    def test_a_pattern_that_does_not_match_is_silent(self):
        alerts.add(self.conn, "Photoshop", "cpu", 80, 300)
        self._samples(self.arc, 95, 11)
        self.assertEqual(alerts.evaluate(self.conn, now=self.now), [])

    def test_the_remainder_never_fires(self):
        # __other__ is hundreds of processes summed. Alerting on it names
        # nothing anyone can act on.
        alerts.add(self.conn, "*", "cpu", 80, 300)
        self._samples(self.other, 99, 11)
        self.assertEqual(alerts.evaluate(self.conn, now=self.now), [])

    def test_it_does_not_fire_again_immediately(self):
        """A runaway lasting an hour is one piece of news, not 120.

        The second window is kept fully covered on purpose: without fresh
        samples it would fall short of the sustain and stay quiet for the
        wrong reason, and the test would pass with the re-arm removed.
        """
        alerts.add(self.conn, "Arc", "cpu", 80, 300)
        self._samples(self.arc, 95, 11)
        self.assertEqual(len(alerts.evaluate(self.conn, now=self.now)), 1)
        later = self.now + 60
        self._samples(self.arc, 95, 13, end=later)
        self.assertEqual(alerts.evaluate(self.conn, now=later), [],
                         "it fired again while the same breach was ongoing")

    def test_it_fires_again_once_rearmed(self):
        alerts.add(self.conn, "Arc", "cpu", 80, 300)
        self._samples(self.arc, 95, 11)
        alerts.evaluate(self.conn, now=self.now)
        later = self.now + alerts.REARM + config.INTERVAL
        self._samples(self.arc, 95, 11, end=later)
        self.assertEqual(len(alerts.evaluate(self.conn, now=later)), 1)

    def test_a_disabled_rule_is_silent(self):
        alerts.add(self.conn, "Arc", "cpu", 80, 300)
        self.conn.execute("UPDATE alert_rule SET enabled = 0")
        self.conn.commit()
        self._samples(self.arc, 95, 11)
        self.assertEqual(alerts.evaluate(self.conn, now=self.now), [])

    def test_events_are_readable_afterwards(self):
        alerts.add(self.conn, "Arc", "cpu", 80, 300)
        self._samples(self.arc, 95, 11)
        alerts.evaluate(self.conn, now=self.now)
        log = alerts.recent(self.conn)
        self.assertEqual(log[0]["exe"], "Arc")
        self.assertEqual(log[0]["unit"], "%")

    def test_no_rules_means_no_work(self):
        self._samples(self.arc, 99, 20)
        self.assertEqual(alerts.evaluate(self.conn, now=self.now), [])


if __name__ == "__main__":
    unittest.main()
