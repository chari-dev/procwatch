import unittest

from procwatch import config, db, query


class TestSearch(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)
        self.ids = {}
        for exe, args, cmd, is_sys, app in [
            ("Arc", "", "/Applications/Arc.app/Contents/MacOS/Arc", 0, "Arc"),
            ("Arc Helper", "--type=renderer",
             "/Applications/Arc.app/.../Arc Helper --type=renderer", 0, "Arc"),
            ("mds_stores", "", "/System/Library/mds_stores", 1, ""),
            ("python3", "", "/opt/homebrew/bin/python3 arc_backup.py", 0, ""),
            ("100%_weird_name", "", "/tmp/100%_weird_name", 0, ""),
            ("never_sampled", "", "/tmp/never_sampled", 0, ""),
        ]:
            self.ids[exe] = self.conn.execute(
                "INSERT INTO proc (exe, args_sig, cmdline_full, is_system, app) "
                "VALUES (?,?,?,?,?)", (exe, args, cmd, is_sys, app)).lastrowid
        rows = [
            # (exe, ts, cpu_max tenths)
            ("Arc", 1000, 800), ("Arc", 2000, 400),
            ("Arc Helper", 1000, 120),
            ("mds_stores", 1000, 50),
            ("python3", 1000, 30),
            ("100%_weird_name", 1000, 10),
        ]
        for exe, ts, cpu in rows:
            self.conn.execute(
                "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, cpu_max_ts, "
                "rss_avg, rss_max, nproc, samples) VALUES (?,?,?,?,?,?,?,?,?)",
                (ts, self.ids[exe], cpu // 2, cpu, ts, 1000, 2000, 1, 1))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def names(self, term, **kw):
        return [r["exe"] for r in query.search(self.conn, term, **kw)]

    def test_it_finds_by_process_name(self):
        self.assertIn("Arc", self.names("Arc"))

    def test_it_is_case_insensitive(self):
        self.assertIn("Arc", self.names("arc"))

    def test_it_finds_by_application(self):
        # The helper's own name does not contain "Arc" as a whole word in a
        # way the user would guess; the app does.
        self.assertIn("Arc Helper", self.names("Arc"))

    def test_it_finds_by_command_line(self):
        # python3 is only related to "arc_backup" through its command.
        self.assertIn("python3", self.names("arc_backup"))

    def test_an_exact_name_outranks_a_command_line_mention(self):
        self.assertEqual(self.names("Arc")[0], "Arc")

    def test_results_carry_what_makes_them_worth_clicking(self):
        hit = query.search(self.conn, "Arc")[0]
        self.assertEqual(hit["cpu_max"], 80.0)      # tenths -> percent
        self.assertEqual(hit["last_ts"], 2000)      # most recent sample
        self.assertEqual(hit["samples"], 2)
        self.assertFalse(hit["is_system"])

    def test_an_identity_that_was_never_sampled_is_not_offered(self):
        # Clicking it would lead to an empty chart.
        self.assertNotIn("never_sampled", self.names("never_sampled"))

    def test_a_percent_sign_is_matched_literally(self):
        # LIKE treats % as "anything". Unescaped, searching for it returns the
        # entire database -- and % is exactly what someone types when hunting
        # for a percentage.
        self.assertEqual(self.names("100%_weird"), ["100%_weird_name"])

    def test_an_underscore_is_matched_literally(self):
        # "_" is LIKE's single-character wildcard; "a_c" must not match "Arc".
        self.assertNotIn("Arc", self.names("a_c"))

    def test_an_empty_search_returns_nothing_rather_than_everything(self):
        self.assertEqual(query.search(self.conn, ""), [])
        self.assertEqual(query.search(self.conn, "   "), [])
        self.assertEqual(query.search(self.conn, None), [])

    def test_the_limit_is_honoured(self):
        self.assertLessEqual(len(query.search(self.conn, "a", limit=2)), 2)

    def test_a_named_match_beats_a_busier_command_line_match(self):
        """Typing a name must answer with that name.

        The busiest process matching "python3" in its command line would
        otherwise outrank python3 itself, which is never what was meant.
        """
        busy = self.conn.execute(
            "INSERT INTO proc (exe, args_sig, cmdline_full, is_system, app) "
            "VALUES ('hog','','/tmp/hog --wrap python3',0,'')").lastrowid
        self.conn.execute(
            "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, cpu_max_ts, "
            "rss_avg, rss_max, nproc, samples) VALUES (5000,?,900,9000,5000,1,1,1,1)",
            (busy,))
        self.conn.commit()
        found = self.names("python3")
        self.assertEqual(found[0], "python3",
                         "a command-line match outranked the process itself")
        self.assertIn("hog", found)

    def test_the_app_itself_outranks_its_busier_helpers(self):
        """Searching "Arc" should answer Arc, not Arc's renderer.

        Helpers share the application name, so an exact match on the app puts
        them level with the app itself -- and renderers routinely out-consume
        the browser process that owns them, so they would always win on CPU.
        """
        helper = self.ids["Arc Helper"]
        self.conn.execute(
            "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, cpu_max_ts, "
            "rss_avg, rss_max, nproc, samples) VALUES (7000,?,900,9500,7000,1,1,1,1)",
            (helper,))
        self.conn.commit()
        found = self.names("Arc")
        self.assertEqual(found[0], "Arc", "a helper outranked the app itself")
        self.assertIn("Arc Helper", found)

    def test_a_prefix_match_beats_a_busier_mention_in_the_middle(self):
        # A name that begins with what you typed is the better answer even
        # when something noisier merely contains it.
        noisy = self.conn.execute(
            "INSERT INTO proc (exe, args_sig, cmdline_full, is_system, app) "
            "VALUES ('spotlight_mds_helper','','/usr/libexec/spotlight_mds_helper',1,'')"
        ).lastrowid
        self.conn.execute(
            "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, cpu_max_ts, "
            "rss_avg, rss_max, nproc, samples) VALUES (5000,?,900,9000,5000,1,1,1,1)",
            (noisy,))
        self.conn.commit()
        found = self.names("mds")
        self.assertEqual(found[0], "mds_stores",
                         "a mid-name match outranked a prefix match")
        self.assertIn("spotlight_mds_helper", found)

    def test_the_busiest_match_comes_first(self):
        # A search for something they all share must lead with the one that
        # actually used the machine.
        found = query.search(self.conn, "/")
        self.assertEqual(found[0]["exe"], "Arc")


if __name__ == "__main__":
    unittest.main()
