import calendar
import json
import os
import shutil
import tempfile
import time
import unittest

from procwatch import db, diagnose, events


def _local(text):
    """A local-time timestamp, for building fixtures the way macOS writes them."""
    parts = time.strptime(text, "%Y-%m-%d %H:%M:%S")
    return int(time.mktime((parts.tm_year, parts.tm_mon, parts.tm_mday,
                            parts.tm_hour, parts.tm_min, parts.tm_sec, 0, 0, -1)))


class TestReportFilenames(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, *names):
        for name in names:
            with open(os.path.join(self.dir, name), "w") as handle:
                handle.write("x")
        return events.read_reports([self.dir])

    def test_a_crash_report_becomes_a_crash(self):
        found = self._write("Arc_2026-07-27-142829_host.ips")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["kind"], "crash")
        self.assertEqual(found[0]["subject"], "Arc")
        self.assertEqual(found[0]["severity"], "fault")

    def test_a_resource_report_is_a_cost_not_a_fault(self):
        # Nothing was stopped and nothing broke: macOS noted that a program
        # went past a budget. Calling it a fault would be crying wolf.
        found = self._write("siriactionsd_2026-07-27-142829_host.cpu_resource.diag")
        self.assertEqual(found[0]["kind"], "cpu-limit")
        self.assertEqual(found[0]["severity"], "cost")

    def test_the_timestamp_is_read_as_local_time(self):
        found = self._write("Arc_2026-07-27-142829_host.ips")
        self.assertEqual(found[0]["ts"], _local("2026-07-27 14:28:29"))

    def test_a_process_name_containing_underscores_survives(self):
        found = self._write("com.apple.WebKit.WebContent_2026-07-27-142829_h.ips")
        self.assertEqual(found[0]["subject"], "com.apple.WebKit.WebContent")

    def test_a_plain_diagnostic_is_not_an_event(self):
        # macOS files analytics .diag files daily. A timeline with one
        # meaningless entry a day in it is a timeline nobody opens.
        self.assertEqual(self._write("analyticsd_2026-07-27-155844_host.diag"), [])

    def test_apples_own_daily_trackers_are_ignored(self):
        self.assertEqual(self._write(
            "proactive_event_tracker-com_apple_Trial_2026-07-23-015230_h.diag"), [])

    def test_every_kind_of_report_is_recognised(self):
        found = self._write(
            "a_2026-07-27-010000_h.panic",
            "b_2026-07-27-020000_h.shutdownStall",
            "c_2026-07-27-030000_h.spin",
            "d_2026-07-27-040000_h.wakeups_resource.diag",
            "e_2026-07-27-050000_h.diskwrites_resource.diag")
        self.assertEqual({f["kind"] for f in found},
                         {"panic", "shutdown-stall", "spin", "wakeups-limit",
                          "disk-limit"})

    def test_an_unreadable_directory_is_not_an_error(self):
        self.assertEqual(events.read_reports(["/nonexistent/place"]), [])

    def test_the_key_is_stable_so_a_second_read_adds_nothing(self):
        first = self._write("Arc_2026-07-27-142829_host.ips")
        second = events.read_reports([self.dir])
        self.assertEqual(first[0]["key"], second[0]["key"])


LAST_REBOOT = """\
reboot time                                Fri Jul 24 22:17
shutdown time                              Fri Jul 24 22:10
reboot time                                Mon Jul 20 21:18
reboot time                                Thu Jan 02 09:00
reboot time                                Mon Dec 30 23:40
"""


class TestBoots(unittest.TestCase):
    def test_boots_and_shutdowns_are_both_read(self):
        found = events.read_boots(now=_local("2026-07-29 12:00:00"),
                                 reboot_text=LAST_REBOOT, boottime=0)
        kinds = [f["kind"] for f in found]
        self.assertEqual(kinds.count("boot"), 4)
        self.assertEqual(kinds.count("shutdown"), 1)

    def test_a_year_boundary_is_inferred_rather_than_assumed(self):
        # `last` prints no year. Assuming the current one dates last
        # December's reboot to next December -- five months in the future.
        found = events.read_boots(now=_local("2026-07-29 12:00:00"),
                                 reboot_text=LAST_REBOOT, boottime=0)
        stamps = sorted(f["ts"] for f in found if f["kind"] == "boot")
        self.assertEqual(time.localtime(stamps[0]).tm_year, 2025)
        self.assertEqual(time.localtime(stamps[0]).tm_mon, 12)
        self.assertEqual(time.localtime(stamps[1]).tm_year, 2026)

    def test_no_boot_is_dated_in_the_future(self):
        now = _local("2026-07-29 12:00:00")
        for row in events.read_boots(now=now, reboot_text=LAST_REBOOT,
                                    boottime=0):
            self.assertLessEqual(row["ts"], now + 86400)

    def test_a_boot_after_a_shutdown_is_not_flagged(self):
        found = events.read_boots(now=_local("2026-07-29 12:00:00"),
                                 reboot_text=LAST_REBOOT, boottime=0)
        boot = [f for f in found
                if f["kind"] == "boot"
                and f["ts"] == _local("2026-07-24 22:17:00")][0]
        self.assertEqual(boot["detail"], "")

    def test_a_boot_with_no_shutdown_says_so_without_claiming_a_fault(self):
        # The absence is evidence, not proof: macOS does not reliably record a
        # shutdown, so announcing every such boot as an unclean shutdown would
        # be a horoscope.
        found = events.read_boots(now=_local("2026-07-29 12:00:00"),
                                 reboot_text=LAST_REBOOT, boottime=0)
        self.assertFalse([f for f in found if f["kind"] == "unclean-shutdown"])
        lonely = [f for f in found
                  if f["kind"] == "boot"
                  and f["ts"] == _local("2026-07-20 21:18:00")][0]
        self.assertIn("not always", lonely["detail"])

    def test_a_panic_before_a_boot_promotes_it_to_a_fault(self):
        boot = _local("2026-07-20 21:18:00")
        found = events.read_boots(now=_local("2026-07-29 12:00:00"),
                                 reboot_text=LAST_REBOOT, boottime=0,
                                 corroborate=[boot - 300])
        bad = [f for f in found if f["kind"] == "unclean-shutdown"]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["severity"], "fault")
        self.assertIn("report", bad[0]["detail"])

    def test_the_running_boot_comes_from_the_kernel_not_from_last(self):
        # `last` rounds to the minute; sample timestamps do not. The current
        # session has to line up with them or it appears to start late.
        exact = _local("2026-07-24 22:17:35")
        found = events.read_boots(now=_local("2026-07-29 12:00:00"),
                                 reboot_text=LAST_REBOOT, boottime=exact)
        self.assertIn(exact, [f["ts"] for f in found if f["kind"] == "boot"])


class TestInstalls(unittest.TestCase):
    def _history(self, *items):
        return json.dumps({"SPInstallHistoryDataType": list(items)})

    def test_the_install_date_is_read_as_utc(self):
        # The one field in this file that is not local time. Reading it as
        # local puts every install hours out, in whichever direction the
        # machine happens to be from Greenwich.
        found = events.read_installs(text=self._history(
            {"_name": "Thing", "install_date": "2026-07-29T08:03:05Z"}))
        self.assertEqual(found[0]["ts"],
                         calendar.timegm(time.strptime("2026-07-29T08:03:05Z",
                                                       "%Y-%m-%dT%H:%M:%SZ")))

    def test_macos_itself_is_marked_as_an_os_update(self):
        found = events.read_installs(text=self._history(
            {"_name": "macOS 27.0", "install_date": "2026-07-24T20:19:00Z",
             "install_version": "27.0"}))
        self.assertEqual(found[0]["kind"], "os-update")
        self.assertEqual(found[0]["severity"], "change")

    def test_malware_definitions_are_recorded_but_not_ranked(self):
        found = events.read_installs(text=self._history(
            {"_name": "XProtectPlistConfigData",
             "install_date": "2026-07-29T08:03:05Z"}))
        self.assertEqual(found[0]["kind"], "security-update")
        self.assertEqual(found[0]["severity"], "note")

    def test_junk_json_is_not_an_error(self):
        self.assertEqual(events.read_installs(text="not json"), [])

    def test_an_entry_with_no_date_is_skipped(self):
        self.assertEqual(events.read_installs(
            text=self._history({"_name": "Thing"})), [])


def _conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    events.init(conn)
    return conn


def _put(conn, rows):
    return events._store(conn, rows)


def _event(ts, kind, subject="", severity="cost", detail=""):
    return {"ts": ts, "kind": kind, "subject": subject, "detail": detail,
            "severity": severity, "source": "test",
            "key": "%s:%s:%s" % (kind, subject, ts)}


class TestStorage(unittest.TestCase):
    def test_storing_the_same_event_twice_records_it_once(self):
        conn = _conn()
        row = _event(1750000000, "crash", "Arc", "fault")
        self.assertEqual(_put(conn, [row]), 1)
        self.assertEqual(_put(conn, [row]), 0)

    def test_re_collection_never_rewrites_what_was_already_recorded(self):
        # The keys are stable, so a second read of the same directory hits the
        # same rows. It must leave them alone: the first observation is the
        # authoritative one, and a collector that later phrases an event
        # differently would otherwise quietly rewrite history. Counting rows
        # cannot catch this -- REPLACE keeps the count identical.
        conn = _conn()
        row = _event(1750000000, "crash", "Arc", "fault", detail="as first seen")
        _put(conn, [row])
        again = dict(row)
        again["detail"] = "rewritten later"
        again["severity"] = "note"
        _put(conn, [again])
        kept = conn.execute("SELECT detail, severity FROM event").fetchall()
        self.assertEqual(kept, [("as first seen", "fault")])

    def test_collection_is_marked_so_it_does_not_repeat_every_tick(self):
        conn = _conn()
        self.assertTrue(events.due(conn, now=1750000000))
        events.collect(conn, now=1750000000, external=False)
        self.assertFalse(events.due(conn, now=1750000000 + 60))
        self.assertTrue(events.due(conn, now=1750000000 + events.COLLECT_EVERY))

    def test_the_database_sources_become_events(self):
        conn = _conn()
        from procwatch import power
        power.init(conn)
        with conn:
            conn.execute("INSERT INTO power_event (ts, kind, reason, charge) "
                         "VALUES (1750000000,'wake','wifibt',77)")
            conn.execute("INSERT INTO gap (ts_start, ts_end, reason) "
                         "VALUES (1750001000,1750004600,'asleep')")
        events.collect(conn, now=1750010000, external=False)
        kinds = {r[0] for r in conn.execute("SELECT kind FROM event")}
        self.assertEqual(kinds, {"wake", "gap"})
        detail = conn.execute("SELECT detail FROM event WHERE kind='wake'"
                              ).fetchone()[0]
        self.assertIn("77%", detail)

    def test_a_first_recorded_version_is_not_an_update(self):
        conn = _conn()
        from procwatch import versions
        versions.init(conn)
        with conn:
            conn.execute("INSERT INTO app_version (app, version, first_ts, "
                         "last_ts) VALUES ('Arc','1.0',1750000000,1750000000)")
        events.collect(conn, now=1750010000, external=False)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM event WHERE kind='app-update'").fetchone()[0], 0)
        with conn:
            conn.execute("INSERT INTO app_version (app, version, first_ts, "
                         "last_ts) VALUES ('Arc','1.1',1750005000,1750005000)")
        events.collect(conn, now=1750020000, external=False)
        row = conn.execute("SELECT subject, detail FROM event "
                           "WHERE kind='app-update'").fetchone()
        self.assertEqual(row[0], "Arc")
        self.assertIn("1.0 to 1.1", row[1])

    def test_pruning_keeps_events_far_longer_than_samples(self):
        conn = _conn()
        now = 1750000000
        _put(conn, [_event(now - 300 * 86400, "crash", "Old", "fault"),
                    _event(now - 500 * 86400, "crash", "Ancient", "fault")])
        events.prune(conn, now=now)
        kept = {r[0] for r in conn.execute("SELECT subject FROM event")}
        self.assertEqual(kept, {"Old"})


class TestEpisodes(unittest.TestCase):
    def test_events_close_together_are_one_incident(self):
        conn = _conn()
        base = 1750000000
        _put(conn, [_event(base, "wake", severity="note"),
                    _event(base + 60, "crash", "Arc", "fault"),
                    _event(base + 120, "cpu-limit", "mds", "cost")])
        groups = events.episodes(conn, base - 10, base + 3600)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 3)

    def test_a_gap_longer_than_the_window_splits_them(self):
        conn = _conn()
        base = 1750000000
        _put(conn, [_event(base, "crash", "Arc", "fault"),
                    _event(base + events.EPISODE_GAP + 60, "crash", "B", "fault")])
        self.assertEqual(len(events.episodes(conn, base - 10, base + 7200)), 2)

    def test_the_incident_is_named_after_what_went_wrong(self):
        # Not after whatever happened first, and not after the highest
        # severity alone -- a wake and a crash in the same minute is a crash.
        conn = _conn()
        base = 1750000000
        _put(conn, [_event(base, "wake", severity="note"),
                    _event(base + 30, "crash", "Arc", "fault")])
        group = events.episodes(conn, base - 10, base + 600)[0]
        self.assertIn("Arc", group["headline"])

    def test_routine_never_titles_an_incident_that_contains_anything_else(self):
        # Ranking by severity alone gets this right only by accident: today
        # every routine kind happens to be the lowest severity there is. The
        # contract is stronger than that -- a sleep does not title an incident
        # that also contains something that is not routine, whatever the two
        # severities are.
        conn = _conn()
        base = 1750000000
        _put(conn, [_event(base, "sleep", severity="note"),
                    _event(base + 30, "app-update", "Arc", "note")])
        group = events.episodes(conn, base - 10, base + 600)[0]
        self.assertIn("Arc", group["headline"])

    def test_an_incident_of_nothing_but_routine_can_be_left_out(self):
        conn = _conn()
        base = 1750000000
        _put(conn, [_event(base, "sleep", severity="note"),
                    _event(base + 30, "wake", severity="note")])
        self.assertEqual(len(events.episodes(conn, base - 10, base + 600)), 1)
        self.assertEqual(
            len(events.episodes(conn, base - 10, base + 600, routine=False)), 0)

    def test_what_else_happened_is_ordered_by_weight_not_by_time(self):
        conn = _conn()
        base = 1750000000
        _put(conn, [_event(base, "panic", severity="fault"),
                    _event(base + 10, "sleep", severity="note"),
                    _event(base + 20, "cpu-limit", "mds", "cost")])
        group = events.episodes(conn, base - 10, base + 600)[0]
        self.assertIn("mds", group["also"][0])


class TestPatterns(unittest.TestCase):
    def _repeats(self, times, kind="cpu-limit", subject="siriactionsd"):
        conn = _conn()
        _put(conn, [_event(ts, kind, subject, "cost") for ts in times])
        return conn

    def test_a_repeat_below_the_threshold_is_not_a_pattern(self):
        conn = self._repeats([1750000000, 1750100000])
        self.assertEqual(events.patterns(conn, 0, 1760000000), [])

    def test_a_repeat_is_counted_and_dated(self):
        stamps = [1750000000 + i * 86400 for i in range(4)]
        conn = self._repeats(stamps)
        found = events.patterns(conn, 0, 1760000000)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["count"], 4)
        self.assertEqual(found[0]["first_ts"], stamps[0])
        self.assertEqual(found[0]["last_ts"], stamps[-1])

    def test_three_inside_ten_minutes_is_reported_as_a_burst(self):
        base = 1750000000
        conn = self._repeats([base, base + 120, base + 240])
        self.assertTrue(events.patterns(conn, 0, 1760000000)[0]["burst"])

    def test_the_same_count_spread_over_weeks_is_not_a_burst(self):
        conn = self._repeats([1750000000 + i * 7 * 86400 for i in range(3)])
        self.assertFalse(events.patterns(conn, 0, 1760000000)[0]["burst"])

    def test_repeats_after_waking_are_counted(self):
        conn = _conn()
        base = 1750000000
        rows = []
        for i in range(3):
            ts = base + i * 86400
            rows.append(_event(ts, "wake", severity="note"))
            rows.append(_event(ts + 60, "cpu-limit", "siriactionsd", "cost"))
        _put(conn, rows)
        found = events.patterns(conn, 0, 1760000000)[0]
        self.assertEqual(found["after_wake"], 3)
        self.assertIn("waking", events.describe_pattern(found, now=base + 300000))

    def test_the_hour_of_day_is_the_local_hour(self):
        # ts % 86400 is the hour in UTC. Claiming "always around 03:00" from it
        # is wrong by the machine's offset everywhere but Greenwich.
        wanted = 3
        stamps = [_local("2026-07-2%d 03:10:00" % d) for d in (1, 2, 3, 4)]
        conn = self._repeats(stamps)
        self.assertEqual(events.patterns(conn, 0, 1790000000)[0]["typical_hour"],
                         wanted)

    def test_events_scattered_through_the_day_claim_no_hour(self):
        stamps = [_local("2026-07-21 %02d:00:00" % h) for h in (2, 9, 16, 22)]
        conn = self._repeats(stamps)
        self.assertIsNone(events.patterns(conn, 0, 1790000000)[0]["typical_hour"])

    def test_events_straddling_midnight_are_one_cluster_not_two(self):
        stamps = [_local("2026-07-21 23:50:00"), _local("2026-07-22 00:10:00"),
                  _local("2026-07-23 00:00:00")]
        conn = self._repeats(stamps)
        found = events.patterns(conn, 0, 1790000000)[0]
        self.assertLessEqual(found["spread_hours"], events.SAME_TIME_HOURS)
        self.assertIn(found["typical_hour"], (0, 23))

    def test_installs_are_changes_and_never_reported_as_repeats(self):
        conn = _conn()
        _put(conn, [_event(1750000000 + i * 86400, "os-update", "macOS 27.0",
                           "change") for i in range(3)])
        self.assertEqual(events.patterns(conn, 0, 1760000000), [])

    def test_a_pattern_carries_what_the_process_is(self):
        conn = self._repeats([1750000000 + i * 86400 for i in range(3)],
                             subject="mds_stores")
        self.assertTrue(events.patterns(conn, 0, 1760000000)[0]["about"]["known"])


class TestFirsts(unittest.TestCase):
    def test_nothing_is_a_first_until_the_history_reaches_back(self):
        # The trap: with nothing older to compare against, every event is its
        # own first occurrence and a fresh install announces forty firsts.
        conn = _conn()
        base = 1750000000
        _put(conn, [_event(base, "crash", "Arc", "fault")])
        self.assertEqual(events.firsts(conn, base - 10, base + 10), [])

    def test_a_new_subject_in_a_deep_history_is_a_first(self):
        conn = _conn()
        base = 1750000000
        _put(conn, [_event(base, "crash", "Old", "fault"),
                    _event(base + 30 * 86400, "crash", "New", "fault")])
        found = events.firsts(conn, base + 30 * 86400 - 10,
                              base + 30 * 86400 + 10)
        self.assertEqual([f["subject"] for f in found], ["New"])

    def test_a_repeat_is_not_a_first(self):
        conn = _conn()
        base = 1750000000
        _put(conn, [_event(base, "crash", "Arc", "fault"),
                    _event(base + 30 * 86400, "crash", "Arc", "fault")])
        self.assertEqual(events.firsts(conn, base + 30 * 86400 - 10,
                                       base + 30 * 86400 + 10), [])


class TestDigest(unittest.TestCase):
    def test_an_empty_window_says_nothing_happened(self):
        conn = _conn()
        digest = events.digest(conn, 1750000000, 1750086400)
        self.assertIn("Nothing", digest["summary"])

    def test_faults_are_named_with_what_happened_to_them(self):
        # "macOS once, shutdown_stall once and WhatsApp once" -- three names, no
        # verb, no way to tell a panic from a crash. The summary has to say what
        # each event was.
        conn = _conn()
        base = 1750000000
        _put(conn, [_event(base, "panic", severity="fault"),
                    _event(base + 60, "crash", "WhatsApp", "fault")])
        summary = events.digest(conn, base - 10, base + 600)["summary"]
        self.assertIn("crashed", summary)
        self.assertIn("WhatsApp", summary)
        self.assertIn("restarted itself", summary)

    def test_resource_reports_are_counted_together(self):
        conn = _conn()
        base = 1750000000
        _put(conn, [_event(base + i, "cpu-limit", "a%d" % i, "cost")
                    for i in range(4)])
        self.assertIn("4 reports",
                      events.digest(conn, base - 10, base + 600)["summary"])

    def test_one_start_reads_as_one_start(self):
        conn = _conn()
        base = 1750000000
        _put(conn, [_event(base, "boot", severity="note"),
                    _event(base - 1, "unclean-shutdown", severity="fault")])
        summary = events.digest(conn, base - 10, base + 600)["summary"]
        self.assertIn("1 start, which was not preceded", summary)

    def test_a_machine_level_event_does_not_carry_a_filename(self):
        conn = _conn()
        base = 1750000000
        _put(conn, [_event(base, "shutdown-stall", "shutdown_stall", "fault")])
        summary = events.digest(conn, base - 10, base + 600)["summary"]
        self.assertNotIn("shutdown_stall", summary)


class TestTimeline(unittest.TestCase):
    def test_a_fault_carries_what_was_busy_at_the_time(self):
        conn = _conn()
        base = 1750000000
        with conn:
            conn.execute("INSERT INTO proc (id, exe, args_sig, cmdline_full, "
                         "app) VALUES (1,'Arc','','','Arc')")
            conn.execute(
                "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, "
                "cpu_max_ts, rss_avg, rss_max, nproc, samples) "
                "VALUES (?,1,1800,1900,?,1000,1000,1,1)", (base, base))
        _put(conn, [_event(base, "crash", "Arc", "fault")])
        row = events.timeline(conn, base - 10, base + 10)[0]
        self.assertEqual(row["busy"][0]["name"], "Arc")
        self.assertEqual(row["busy"][0]["cpu"], 190.0)

    def test_a_routine_event_is_not_given_context_it_does_not_need(self):
        conn = _conn()
        base = 1750000000
        _put(conn, [_event(base, "sleep", severity="note")])
        self.assertEqual(events.timeline(conn, base - 10, base + 10)[0]["busy"],
                         [])

    def test_a_process_event_carries_what_the_process_is(self):
        conn = _conn()
        base = 1750000000
        _put(conn, [_event(base, "cpu-limit", "mds_stores", "cost")])
        row = events.timeline(conn, base - 10, base + 10)[0]
        self.assertEqual(row["about"]["name"], "Spotlight")


class TestVerdict(unittest.TestCase):
    def _machine(self, rows):
        conn = _conn()
        base = 1750000000
        with conn:
            conn.execute("INSERT INTO proc (id, exe, args_sig, cmdline_full, "
                         "app, is_system) VALUES (1,'idle','','','',1)")
            for i in range(20):
                ts = base + i * 30
                conn.execute(
                    "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, "
                    "cpu_max_ts, rss_avg, rss_max, nproc, samples) "
                    "VALUES (?,1,10,10,?,1000,1000,1,1)", (ts, ts))
                conn.execute(
                    "INSERT INTO system_raw (ts, cpu_busy, load1, mem_used_kb, "
                    "mem_comp_kb, swap_used_kb, disk_free_kb, samples, expected)"
                    " VALUES (?,100,100,8000000,500000,0,100000000,1,1)", (ts,))
        _put(conn, rows)
        return conn, base, base + 20 * 30

    def test_a_crash_becomes_a_finding_the_charts_could_never_show(self):
        conn, start, end = self._machine([])
        _put(conn, [_event(start + 60, "crash", "WhatsApp", "fault")])
        found = diagnose.explain(conn, start, end, now=end)["findings"]
        crash = [f for f in found if f["kind"] == "event-crash"]
        self.assertEqual(len(crash), 1)
        self.assertEqual(crash[0]["severity"], "cause")
        self.assertIn("WhatsApp", crash[0]["headline"])

    def test_a_repeat_is_reported_as_a_repeat(self):
        conn, start, end = self._machine([])
        _put(conn, [_event(start - i * 86400, "cpu-limit", "siriactionsd", "cost")
                    for i in range(4)])
        found = diagnose.explain(conn, start, end, now=end)["findings"]
        repeat = [f for f in found if f["kind"] == "event-repeat"]
        self.assertTrue(repeat)
        self.assertIn("not for the first time", repeat[0]["headline"])
        self.assertEqual(repeat[0]["evidence"]["count"], 4)

    def test_a_quiet_machine_with_no_events_still_says_it_was_fine(self):
        conn, start, end = self._machine([])
        result = diagnose.explain(conn, start, end, now=end)
        self.assertFalse([f for f in result["findings"]
                          if f["kind"].startswith("event-")])

    def test_a_database_without_the_event_tables_does_not_break_the_verdict(self):
        # An older database restored from a backup has no event table. A
        # missing history is a missing finding, not a failed verdict.
        conn, start, end = self._machine([])
        with conn:
            conn.execute("DROP TABLE event")
        result = diagnose.explain(conn, start, end, now=end)
        self.assertIn("verdict", result)


if __name__ == "__main__":
    unittest.main()
