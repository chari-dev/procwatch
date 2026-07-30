"""Ending a process you found in the past.

The drill-down shows a minute that has gone. The question it provokes -- "is
that still happening, and can I stop it" -- can only be answered against what is
running now, and the matching is the whole risk: aim it wrongly and a button
labelled with one program's name ends another.
"""
import unittest

from procwatch import db, procs, server


class Proc(object):
    """The shape psreader.read() returns, as much of it as matters here."""

    def __init__(self, pid, comm, command):
        self.pid = pid
        self.comm = comm
        self.command = command
        self.rss_kb = 1000
        self.cputime_cs = 0
        self.start_time = 0


def _db(rows):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    base = 1750000000
    with conn:
        for i, (exe, args_sig, cmdline, app) in enumerate(rows, start=1):
            conn.execute("INSERT INTO proc (id, exe, args_sig, cmdline_full, "
                         "app, is_system) VALUES (?,?,?,?,?,0)",
                         (i, exe, args_sig, cmdline, app))
            conn.execute(
                "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, "
                "cpu_max_ts, rss_avg, rss_max, nproc, samples) "
                "VALUES (?,?,100,100,?,1000,1000,1,1)", (base, i, base))
    return conn, base


class TestMatching(unittest.TestCase):
    def _bucket(self, conn, ts):
        return server.api_get(conn, "/api/bucket",
                              {"tier": ["raw"], "ts": [str(ts)]})

    def test_a_row_is_matched_to_what_is_running_under_the_same_identity(self):
        conn, ts = _db([("claude", "--resume", "claude --resume", "")])
        live = [Proc(4242, "claude", "claude --resume")]
        by_identity, by_app = procs.running_now(live)
        rows = server._attach_live_pids(self._bucket(conn, ts),
                                        by_identity=by_identity)
        self.assertEqual(rows[0]["pids"], [4242])

    def test_the_same_program_with_different_arguments_is_a_different_row(self):
        """The bug this guards against ends the wrong process.

        `claude --resume` and `claude --dangerously-skip-permissions` share an
        executable and are separate rows. Matching on the executable alone puts
        every claude's pid on both, and Quit on one row ends the other.
        """
        conn, ts = _db([
            ("claude", "--resume", "claude --resume", ""),
            ("claude", "--skip", "claude --skip", ""),
        ])
        live = [Proc(11, "claude", "claude --resume"),
                Proc(22, "claude", "claude --skip")]
        by_identity, _ = procs.running_now(live)
        rows = server._attach_live_pids(self._bucket(conn, ts),
                                        by_identity=by_identity)
        found = {r["args_sig"]: r["pids"] for r in rows}
        self.assertEqual(found["--resume"], [11])
        self.assertEqual(found["--skip"], [22])

    def test_no_pid_is_ever_claimed_by_two_rows(self):
        conn, ts = _db([
            ("claude", "--resume", "claude --resume", ""),
            ("claude", "--skip", "claude --skip", ""),
        ])
        live = [Proc(11, "claude", "claude --resume"),
                Proc(22, "claude", "claude --skip")]
        by_identity, _ = procs.running_now(live)
        rows = server._attach_live_pids(self._bucket(conn, ts),
                                        by_identity=by_identity)
        seen = {}
        for row in rows:
            for pid in row["pids"]:
                self.assertNotIn(pid, seen,
                                 "pid %d claimed by %s and %s"
                                 % (pid, seen.get(pid), row["exe"]))
                seen[pid] = row["exe"]

    def test_something_that_has_since_exited_offers_nothing(self):
        # The honest answer, and the one that keeps a button from acting on a
        # process that no longer exists -- or worse, on whatever inherited its
        # number.
        conn, ts = _db([("gone", "", "gone", "")])
        by_identity, _ = procs.running_now([Proc(7, "other", "other")])
        rows = server._attach_live_pids(self._bucket(conn, ts),
                                        by_identity=by_identity)
        self.assertEqual(rows[0]["pids"], [])

    def test_every_copy_running_under_one_identity_is_offered(self):
        conn, ts = _db([("worker", "--go", "worker --go", "")])
        live = [Proc(9, "worker", "worker --go"),
                Proc(3, "worker", "worker --go"),
                Proc(5, "worker", "worker --go")]
        by_identity, _ = procs.running_now(live)
        rows = server._attach_live_pids(self._bucket(conn, ts),
                                        by_identity=by_identity)
        self.assertEqual(rows[0]["pids"], [3, 5, 9])

    def test_the_remainder_band_is_never_actionable(self):
        # It is a sum of processes the recorder never kept separately, so there
        # is nothing it could name, let alone signal.
        from procwatch import config
        conn, ts = _db([(config.OTHER, "", "", "")])
        by_identity, _ = procs.running_now([Proc(7, config.OTHER, "")])
        rows = server._attach_live_pids(self._bucket(conn, ts),
                                        by_identity=by_identity)
        self.assertEqual(rows[0]["pids"], [])

    def test_a_failure_to_read_the_process_list_costs_the_buttons_only(self):
        # `ps` failing is not a reason to lose the minute somebody was reading.
        conn, ts = _db([("thing", "", "thing", "")])

        def explode():
            raise OSError("no ps today")

        was = procs.running_now
        procs.running_now = explode
        try:
            rows = self._bucket(conn, ts)
        finally:
            procs.running_now = was
        self.assertTrue(rows)
        self.assertEqual(rows[0]["exe"], "thing")


class TestRunningNow(unittest.TestCase):
    def test_identities_are_derived_the_same_way_the_sampler_derives_them(self):
        # If these two ever disagree, every match silently becomes a miss and
        # the buttons quietly stop appearing.
        from procwatch import identity
        live = [Proc(1, "thing", "/usr/bin/thing --flag")]
        by_identity, _ = procs.running_now(live)
        self.assertEqual(list(by_identity), [identity.derive("thing",
                                                             "/usr/bin/thing --flag")])


if __name__ == "__main__":
    unittest.main()
