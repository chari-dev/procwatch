import unittest

from procwatch import db, diagnose, knowledge, query, server


class TestCatalogue(unittest.TestCase):
    def test_every_entry_answers_all_four_questions(self):
        # An entry missing `advice` renders an empty section; one missing `high`
        # is worse -- it is the field that separates "this is Spotlight" from
        # "and being busy is expected".
        for name, entry in knowledge.CATALOGUE.items():
            for field in ("name", "cat", "does", "high", "advice"):
                self.assertIn(field, entry, "%s lacks %s" % (name, field))
            self.assertTrue(entry["does"], name)
            self.assertTrue(entry["high"], name)

    def test_daemons_are_translated_rather_than_repeated(self):
        # The whole point is translation. Some names are already human --
        # XProtect, Dropbox, Finder -- but a lowercase daemon name never is,
        # and an entry that just repeats "mds_stores" has translated nothing.
        for name, entry in knowledge.CATALOGUE.items():
            if name.islower():
                self.assertNotEqual(entry["name"], name)

    def test_every_sentence_ends(self):
        for name, entry in knowledge.CATALOGUE.items():
            for field in ("does", "high"):
                self.assertTrue(entry[field].rstrip().endswith("."),
                                "%s.%s is unterminated" % (name, field))

    def test_categories_are_ones_the_interface_knows(self):
        allowed = {knowledge.APPLE, knowledge.THIRD, knowledge.DEV,
                   knowledge.BROWSER}
        for name, entry in knowledge.CATALOGUE.items():
            self.assertIn(entry["cat"], allowed, name)

    def test_it_covers_the_processes_people_actually_ask_about(self):
        # The list a search engine is asked about most. Coverage of these is
        # the difference between a catalogue and a gesture.
        for name in ("kernel_task", "WindowServer", "mds_stores", "mdworker",
                     "backupd", "photoanalysisd", "bird", "cloudd", "coreaudiod",
                     "syspolicyd", "trustd", "mDNSResponder", "nsurlsessiond",
                     "hidd", "cfprefsd", "fseventsd", "softwareupdated"):
            self.assertTrue(knowledge.describe(name)["known"], name)


class TestDescribe(unittest.TestCase):
    def test_a_full_path_is_looked_up_by_its_basename(self):
        d = knowledge.describe("/usr/libexec/mds_stores")
        self.assertEqual(d["name"], "Spotlight")
        self.assertTrue(d["known"])

    def test_kernel_task_is_explained_as_heat_rather_than_a_program(self):
        # The single most misread number on a Mac.
        d = knowledge.describe("kernel_task")
        self.assertIn("cool", d["does"] + d["high"] + d["advice"])

    def test_an_unknown_renderer_is_recognised_as_a_tab(self):
        d = knowledge.describe("Arc Helper (Renderer)", app="Arc")
        self.assertFalse(d["known"])
        self.assertIn("tab", d["does"])
        self.assertIn("Arc", d["does"])

    def test_a_gpu_helper_is_not_reported_as_a_tab(self):
        d = knowledge.describe("Slack Helper (GPU)", app="Slack")
        self.assertIn("graphics", d["name"] + d["does"])

    def test_an_unknown_daemon_is_described_as_a_daemon_and_marked_a_guess(self):
        d = knowledge.describe("wibbled")
        self.assertFalse(d["known"])
        self.assertIn("background", d["does"])

    def test_a_binary_in_the_system_is_identified_from_its_path(self):
        d = knowledge.describe("Thing", "/System/Library/Thing/Thing --serve")
        self.assertIn("macOS", d["name"])
        self.assertFalse(d["known"])

    def test_a_homebrew_binary_is_identified_as_self_installed(self):
        d = knowledge.describe("weird", "/opt/homebrew/bin/weird")
        self.assertIn("yourself", d["name"] + d["does"])

    def test_something_it_cannot_place_says_so_rather_than_inventing(self):
        # The failure that matters: a confident wrong answer teaches people to
        # stop reading the right ones.
        d = knowledge.describe("Zyxwv", "/Users/me/Zyxwv")
        self.assertFalse(d["known"])
        self.assertIn("Not something this knows about", d["does"])

    def test_the_process_name_comes_back_for_the_interface_to_key_on(self):
        self.assertEqual(knowledge.describe("/x/y/hidd")["process"], "hidd")

    def test_a_known_entry_is_never_marked_a_guess(self):
        self.assertTrue(knowledge.describe("bird")["known"])


def _db(rows=()):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    with conn:
        conn.execute("INSERT INTO proc (id, exe, args_sig, cmdline_full, app, "
                     "is_system) VALUES (1,'WindowServer','','','',1)")
        for i, (cpu, rss) in enumerate(rows):
            conn.execute(
                "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, "
                "cpu_max_ts, rss_avg, rss_max, nproc, samples) "
                "VALUES (?,1,?,?,?,?,?,1,1)",
                (1750000000 + i * 30, cpu, cpu, 1750000000, rss, rss))
    return conn


class TestUsual(unittest.TestCase):
    def test_it_reports_the_baseline_for_this_machine(self):
        conn = _db([(100, 102400), (300, 204800)])
        u = query.usual(conn, "WindowServer")
        self.assertEqual(u["cpu_avg"], 20.0)
        self.assertEqual(u["cpu_peak"], 30.0)
        self.assertEqual(u["samples"], 2)

    def test_a_process_never_seen_has_no_baseline(self):
        self.assertIsNone(query.usual(_db(), "nothing"))

    def test_the_endpoint_answers_with_the_catalogue_and_the_baseline_together(self):
        conn = _db([(100, 102400)])
        d = server.api_get(conn, "/api/what", {"name": ["WindowServer"]})
        self.assertEqual(d["name"], "The display system")
        self.assertEqual(d["usual"]["samples"], 1)

    def test_the_endpoint_answers_for_something_it_does_not_know(self):
        d = server.api_get(conn=_db(), path="/api/what", params={"name": ["zzz"]})
        self.assertFalse(d["known"])
        self.assertIsNone(d["usual"])


class TestVerdictUsesIt(unittest.TestCase):
    def _busy(self, exe, app=""):
        conn = db.connect(":memory:")
        db.init_schema(conn)
        with conn:
            conn.execute("INSERT INTO proc (id, exe, args_sig, cmdline_full, "
                         "app, is_system) VALUES (1,?,'','',?,1)", (exe, app))
            for i in range(30):
                ts = 1750000000 + i * 30
                conn.execute(
                    "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, "
                    "cpu_max_ts, rss_avg, rss_max, nproc, samples) "
                    "VALUES (?,1,1800,1800,?,102400,102400,1,1)", (ts, ts))
                conn.execute(
                    "INSERT INTO system_raw (ts, cpu_busy, load1, mem_used_kb, "
                    "mem_comp_kb, swap_used_kb, disk_free_kb, samples, "
                    "expected) VALUES (?,6000,100,8000000,500000,0,"
                    "100000000,1,1)", (ts,))
        return conn, 1750000000, 1750000000 + 30 * 30

    def test_a_macos_process_is_never_reported_as_an_app_to_quit(self):
        # hidd is not in SYSTEM_WORK, so before the catalogue it came out as
        # "hidd was working hard -- quit and reopen it", which is impossible
        # advice about a part of the operating system.
        conn, start, end = self._busy("hidd")
        found = diagnose.explain(conn, start, end, now=end)["findings"]
        kinds = [f["kind"] for f in found]
        self.assertIn("system-known", kinds)
        self.assertNotIn("busy-app", kinds)
        one = [f for f in found if f["kind"] == "system-known"][0]
        self.assertIn("Input devices", one["headline"])
        self.assertNotIn("Quit and reopen", one["advice"])

    def test_a_finding_carries_what_the_process_is(self):
        conn, start, end = self._busy("mds_stores")
        found = diagnose.explain(conn, start, end, now=end)["findings"]
        one = [f for f in found if f["kind"] == "system-work"][0]
        self.assertEqual(one["about"]["process"], "mds_stores")
        self.assertTrue(one["about"]["known"])

    def test_a_guess_never_overrides_the_advice_for_an_application(self):
        # Findings are grouped by application, so the process standing in for
        # "Arc" can be one of its renderers. Its shape advice -- "look at the
        # application it belongs to" -- is nonsense said about Arc itself.
        conn, start, end = self._busy("Arc Helper (Renderer)", app="Arc")
        found = diagnose.explain(conn, start, end, now=end)["findings"]
        one = [f for f in found if f["kind"] == "busy-app"][0]
        self.assertNotIn("belongs to", one["advice"])

    def test_a_third_party_app_still_gets_the_advice_it_had(self):
        conn, start, end = self._busy("Whatever", app="Whatever")
        found = diagnose.explain(conn, start, end, now=end)["findings"]
        kinds = [f["kind"] for f in found]
        self.assertIn("busy-app", kinds)


if __name__ == "__main__":
    unittest.main()
