"""Being told when something new turns up, once.

The verdict is computed when somebody opens the dashboard. That answers a
question they asked; it cannot tell them their Mac has started doing something
worth knowing about while they were not looking. The recorder works it out on a
slow cadence and says so -- and everything interesting here is about NOT saying
it: not on the first pass, not twice, and not at all when asked to be quiet.
"""
import unittest

from procwatch import db, diagnose, prefs


def _busy(conn, start, minutes=15, cpu=1800):
    """A stretch where one application is holding 180% of a core."""
    with conn:
        for i in range(minutes * 2):
            ts = start + i * 30
            conn.execute(
                "INSERT OR REPLACE INTO sample_raw (ts, proc_id, cpu_avg, "
                "cpu_max, cpu_max_ts, rss_avg, rss_max, nproc, samples) "
                "VALUES (?,1,?,?,?,102400,102400,1,1)", (ts, cpu, cpu, ts))
            conn.execute(
                "INSERT OR REPLACE INTO system_raw (ts, cpu_busy, load1, "
                "mem_used_kb, mem_comp_kb, swap_used_kb, disk_free_kb, samples, "
                "expected) VALUES (?,6000,100,8000000,500000,0,100000000,1,1)",
                (ts,))


def _second_app(conn, start, minutes=15):
    """A second application misbehaving, so there is more than one finding."""
    with conn:
        conn.execute("INSERT OR IGNORE INTO proc (id, exe, args_sig, "
                     "cmdline_full, app, is_system) "
                     "VALUES (2,'badger','','','Badger',0)")
        for i in range(minutes * 2):
            ts = start + i * 30
            conn.execute(
                "INSERT OR REPLACE INTO sample_raw (ts, proc_id, cpu_avg, "
                "cpu_max, cpu_max_ts, rss_avg, rss_max, nproc, samples) "
                "VALUES (?,2,1900,1900,?,102400,102400,1,1)", (ts, ts))


def _machine(findings_cpu=None):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    base = 1750000000
    with conn:
        conn.execute("INSERT INTO proc (id, exe, args_sig, cmdline_full, app, "
                     "is_system) VALUES (1,'hoglet','','','Hoglet',0)")
    _busy(conn, base, cpu=findings_cpu or 1800)
    return conn, base + 30 * 30


class Recorder(object):
    """Stands in for the notification centre."""

    def __init__(self):
        self.sent = []

    def __call__(self, title, body):
        self.sent.append((title, body))


class TestFirstPass(unittest.TestCase):
    def test_the_first_pass_says_nothing(self):
        # Everything true at the moment this is switched on is new to the table
        # and none of it is news to the person, who has been using the machine
        # all along. A backlog emptied onto the screen is how a notification
        # feature gets switched off within a minute of being switched on.
        conn, now = _machine()
        post = Recorder()
        told = diagnose.watch(conn, now=now, post=post)
        self.assertEqual(told, [])
        self.assertEqual(post.sent, [])

    def test_but_it_remembers_what_it_saw(self):
        conn, now = _machine()
        diagnose.watch(conn, now=now, post=Recorder())
        seen = conn.execute("SELECT COUNT(*) FROM finding_seen").fetchone()[0]
        self.assertGreater(seen, 0)


class TestNotifying(unittest.TestCase):
    def _prime(self, conn, now):
        """Get past the silent first pass with an unrelated finding recorded."""
        with conn:
            conn.execute("INSERT INTO finding_seen (key, first_ts, last_ts, "
                         "told_ts) VALUES ('something-else',?,?,?)",
                         (now - 86400, now - 86400, now - 86400))

    def test_a_new_finding_is_announced(self):
        conn, now = _machine()
        self._prime(conn, now)
        post = Recorder()
        told = diagnose.watch(conn, now=now, post=post)
        self.assertTrue(told)
        self.assertEqual(len(post.sent), len(told))

    def test_the_notification_carries_the_headline_and_the_advice(self):
        # A notification with only a headline is a shrug with a title.
        conn, now = _machine()
        self._prime(conn, now)
        post = Recorder()
        told = diagnose.watch(conn, now=now, post=post)
        title, body = post.sent[0]
        self.assertEqual(title, told[0]["headline"])
        self.assertTrue(body)

    def test_it_does_not_say_the_same_thing_twice(self):
        conn, now = _machine()
        self._prime(conn, now)
        first = Recorder()
        diagnose.watch(conn, now=now, post=first)
        again = Recorder()
        diagnose.watch(conn, now=now + 600, post=again)
        self.assertTrue(first.sent)
        self.assertEqual(again.sent, [])

    def test_it_speaks_up_again_once_the_news_is_stale(self):
        conn, now = _machine()
        self._prime(conn, now)
        diagnose.watch(conn, now=now, post=Recorder())
        # The same thing happening again, later. Without samples in the second
        # window there is nothing to find and the test would pass for the wrong
        # reason -- silence because nothing happened, not because it was said
        # already.
        _busy(conn, now + diagnose.REARM - 600)
        later = Recorder()
        diagnose.watch(conn, now=now + diagnose.REARM + 60, post=later)
        self.assertTrue(later.sent)

    def test_the_key_ignores_the_numbers_in_the_headline(self):
        # The headline carries durations and clock times, so keying on it would
        # make every pass a new finding and every pass a notification.
        finding = {"kind": "busy-app", "severity": "cause",
                   "headline": "Hoglet was working hard",
                   "evidence": {"application": "Hoglet", "peak_cpu": 190}}
        other = dict(finding, headline="Hoglet was working hard again",
                     evidence={"application": "Hoglet", "peak_cpu": 240})
        self.assertEqual(diagnose._finding_key(finding),
                         diagnose._finding_key(other))

    def test_two_different_processes_are_two_findings(self):
        a = {"kind": "busy-app", "evidence": {"application": "A"}}
        b = {"kind": "busy-app", "evidence": {"application": "B"}}
        self.assertNotEqual(diagnose._finding_key(a), diagnose._finding_key(b))


class TestPreferences(unittest.TestCase):
    def _prime(self, conn, now):
        with conn:
            conn.execute("INSERT INTO finding_seen (key, first_ts, last_ts, "
                         "told_ts) VALUES ('x',?,?,?)", (now, now, now))

    def test_off_means_nothing_is_said(self):
        conn, now = _machine()
        self._prime(conn, now)
        prefs.set(conn, "findings_notify", "off")
        post = Recorder()
        self.assertEqual(diagnose.watch(conn, now=now, post=post), [])
        self.assertEqual(post.sent, [])

    def test_causes_only_leaves_out_the_merely_costly(self):
        conn, now = _machine()
        self._prime(conn, now)
        prefs.set(conn, "findings_notify", "causes")
        told = diagnose.watch(conn, now=now, post=Recorder())
        for finding in told:
            self.assertEqual(finding["severity"], "cause")

    def test_all_includes_the_costs_too(self):
        conn, now = _machine()
        self._prime(conn, now)
        prefs.set(conn, "findings_notify", "all")
        told = diagnose.watch(conn, now=now, post=Recorder())
        self.assertTrue(any(f["severity"] == "cost" for f in told)
                        or all(f["severity"] == "cause" for f in told))
        # Whatever this machine produced, nothing outside the two is announced.
        for finding in told:
            self.assertIn(finding["severity"], ("cause", "cost"))

    def test_disabling_findings_stops_the_work_entirely(self):
        conn, now = _machine()
        self._prime(conn, now)
        prefs.set(conn, "findings_enabled", "0")
        post = Recorder()
        self.assertEqual(diagnose.watch(conn, now=now, post=post), [])
        self.assertEqual(post.sent, [])

    def test_disabled_does_not_even_record_a_pass(self):
        # So switching it back on does not have to wait out an interval before
        # anything happens.
        conn, now = _machine()
        prefs.set(conn, "findings_enabled", "0")
        diagnose.watch(conn, now=now, post=Recorder())
        self.assertTrue(diagnose.watch_due(conn, now=now + 5))

    def test_forgetting_makes_the_next_pass_quiet_again(self):
        # Switching it off and on again months later must not compare against a
        # table from before, or it stays silent about things that are new now.
        conn, now = _machine()
        self._prime(conn, now)
        diagnose.watch(conn, now=now, post=Recorder())
        diagnose.forget_findings(conn)
        # Findings in the later window too, so silence can only be because the
        # table was emptied -- not because there was nothing to say. Without
        # this the test passed with forget_findings gutted.
        _busy(conn, now + 86400 - 600)
        post = Recorder()
        self.assertEqual(diagnose.watch(conn, now=now + 86400, post=post), [])
        self.assertEqual(post.sent, [])
        # And having learned them quietly, it will speak next time.
        _busy(conn, now + 86400 + diagnose.REARM - 600)
        again = Recorder()
        diagnose.watch(conn, now=now + 86400 + diagnose.REARM + 60, post=again)
        self.assertTrue(again.sent)


class TestCadence(unittest.TestCase):
    def test_it_is_due_before_it_has_ever_run(self):
        conn, now = _machine()
        self.assertTrue(diagnose.watch_due(conn, now=now))

    def test_and_not_again_immediately(self):
        conn, now = _machine()
        diagnose.watch(conn, now=now, post=Recorder())
        self.assertFalse(diagnose.watch_due(conn, now=now + 60))
        self.assertTrue(diagnose.watch_due(conn,
                                           now=now + diagnose.WATCH_EVERY))


class TestQuoting(unittest.TestCase):
    def test_a_process_name_cannot_become_a_command(self):
        # The notification goes through osascript, so the name is interpolated
        # into AppleScript. json.dumps is what makes it a string literal.
        import inspect
        from procwatch import alerts
        source = inspect.getsource(alerts.post)
        self.assertIn("json.dumps(body)", source)
        self.assertIn("json.dumps(title)", source)


class TestUnreadCount(unittest.TestCase):
    """What the menu bar shows, and when it stops showing it."""

    def test_findings_start_unread(self):
        conn, now = _machine()
        count, keys = diagnose.unread(conn, now=now)
        self.assertGreater(count, 0)
        self.assertEqual(count, len(keys))

    def test_reading_them_empties_the_count(self):
        conn, now = _machine()
        diagnose.mark_read(conn, now=now)
        self.assertEqual(diagnose.unread(conn, now=now)[0], 0)

    def test_something_new_afterwards_counts_again(self):
        conn, now = _machine()
        diagnose.mark_read(conn, now=now)
        # A different application misbehaving later is a new finding, and the
        # count has to come back or the badge is decoration.
        with conn:
            conn.execute("INSERT INTO proc (id, exe, args_sig, cmdline_full, "
                         "app, is_system) VALUES (2,'other','','','Other',0)")
            for i in range(30):
                ts = now + 60 + i * 30
                conn.execute(
                    "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, "
                    "cpu_max_ts, rss_avg, rss_max, nproc, samples) "
                    "VALUES (?,2,1800,1800,?,102400,102400,1,1)", (ts, ts))
                conn.execute(
                    "INSERT INTO system_raw (ts, cpu_busy, load1, mem_used_kb, "
                    "mem_comp_kb, swap_used_kb, disk_free_kb, samples, expected) "
                    "VALUES (?,6000,100,8000000,500000,0,100000000,1,1)", (ts,))
        later = now + 60 + 30 * 30
        self.assertGreater(diagnose.unread(conn, now=later)[0], 0)

    def test_only_the_keys_given_are_marked(self):
        # So a finding that turns up between the panel being drawn and the
        # request arriving is not silently marked as read.
        conn, now = _machine()
        _second_app(conn, now - 15 * 60)
        count, keys = diagnose.unread(conn, now=now)
        self.assertGreater(count, 1)
        diagnose.mark_read(conn, keys[:1], now=now)
        self.assertEqual(diagnose.unread(conn, now=now)[0], count - 1)

    def test_a_finding_that_has_stopped_being_true_stops_counting(self):
        # Computed from the current verdict rather than from the table, or the
        # count would only ever grow.
        conn, now = _machine()
        self.assertGreater(diagnose.unread(conn, now=now)[0], 0)
        quiet = now + 86400          # a day later, nothing recorded
        self.assertEqual(diagnose.unread(conn, now=quiet)[0], 0)

    def test_the_count_is_zero_when_findings_are_switched_off(self):
        conn, now = _machine()
        prefs.set(conn, "findings_enabled", "0")
        self.assertEqual(diagnose.unread(conn, now=now), (0, []))

    def test_the_endpoint_reports_it(self):
        from procwatch import server
        conn, now = _machine()
        out = server.api_get(conn, "/api/badge", {})
        self.assertIn("count", out)
        self.assertTrue(out["enabled"])


if __name__ == "__main__":
    unittest.main()
