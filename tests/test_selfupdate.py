"""Checking for and installing a newer procwatch.

Nothing in here touches the network or git: _fetch and subprocess.run are
replaced, because the refusals are the point -- a test that needs GitHub to
pass is a test of GitHub.
"""
import os
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from procwatch import config, selfupdate


def _reset_cache():
    selfupdate._CACHE.update({"ts": 0.0, "answer": None})


CONFIG_TEXT = '"""Tunables."""\nVERSION = "9.9.9"\n\nINTERVAL = 30\n'


class TestVersions(unittest.TestCase):
    def test_newer_understands_double_digits(self):
        self.assertTrue(selfupdate._newer("1.10.0", "1.9.9"))
        self.assertFalse(selfupdate._newer("1.9.9", "1.10.0"))

    def test_equal_is_not_newer(self):
        self.assertFalse(selfupdate._newer("1.4.0", "1.4.0"))

    def test_garbage_is_not_newer(self):
        self.assertFalse(selfupdate._newer(None, "1.4.0"))
        self.assertFalse(selfupdate._newer("banana", "1.4.0"))

    def test_parse_version(self):
        self.assertEqual(selfupdate._parse_version(CONFIG_TEXT), "9.9.9")
        self.assertIsNone(selfupdate._parse_version("nothing here"))


class TestCheck(unittest.TestCase):
    def setUp(self):
        _reset_cache()
        self.addCleanup(_reset_cache)

    def test_newer_version_is_reported(self):
        with mock.patch.object(selfupdate, "_fetch",
                               return_value=CONFIG_TEXT):
            answer = selfupdate.check(force=True)
        self.assertTrue(answer["newer"])
        self.assertEqual(answer["latest"], "9.9.9")
        self.assertEqual(answer["current"], config.VERSION)
        self.assertEqual(answer["error"], "")

    def test_same_version_is_quiet(self):
        text = 'VERSION = "%s"\n' % config.VERSION
        with mock.patch.object(selfupdate, "_fetch", return_value=text):
            answer = selfupdate.check(force=True)
        self.assertFalse(answer["newer"])

    def test_network_failure_is_an_answer_not_an_exception(self):
        with mock.patch.object(selfupdate, "_fetch",
                               side_effect=OSError("no route")):
            answer = selfupdate.check(force=True)
        self.assertFalse(answer["newer"])
        self.assertIn("no route", answer["error"])

    def test_answer_is_cached(self):
        with mock.patch.object(selfupdate, "_fetch",
                               return_value=CONFIG_TEXT) as fetch:
            selfupdate.check(force=True)
            selfupdate.check()
        self.assertEqual(fetch.call_count, 1)

    def test_force_asks_again(self):
        with mock.patch.object(selfupdate, "_fetch",
                               return_value=CONFIG_TEXT) as fetch:
            selfupdate.check(force=True)
            selfupdate.check(force=True)
        self.assertEqual(fetch.call_count, 2)

    def test_unreadable_version_is_named(self):
        with mock.patch.object(selfupdate, "_fetch",
                               return_value="not a config"):
            answer = selfupdate.check(force=True)
        self.assertFalse(answer["newer"])
        self.assertIn("version", answer["error"])


BUNDLE_TEXT = ('#!/usr/bin/env python3\n'
               '"""procwatch -- per-process history for macOS."""\n'
               'VERSION = "9.9.9"\n')


class TestApplyBundle(unittest.TestCase):
    def setUp(self):
        _reset_cache()
        self.addCleanup(_reset_cache)
        handle, self.target = tempfile.mkstemp(suffix=".py")
        os.write(handle, b"# old copy\n")
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(self.target)
                        and os.remove(self.target))

    def _apply(self, downloaded):
        with mock.patch.object(selfupdate, "mode", return_value="bundle"), \
                mock.patch.object(selfupdate, "_bundle_target",
                                  return_value=self.target), \
                mock.patch.object(selfupdate, "_fetch",
                                  return_value=downloaded):
            return selfupdate.apply()

    def test_replaces_the_file_and_keeps_it_executable(self):
        answer = self._apply(BUNDLE_TEXT)
        self.assertTrue(answer["ok"], answer["error"])
        self.assertEqual(answer["to"], "9.9.9")
        self.assertTrue(answer["restart"])
        with open(self.target) as handle:
            self.assertEqual(handle.read(), BUNDLE_TEXT)
        self.assertTrue(os.stat(self.target).st_mode & stat.S_IXUSR)

    def test_refuses_something_that_is_not_procwatch(self):
        answer = self._apply("<html>rate limited</html>")
        self.assertFalse(answer["ok"])
        with open(self.target) as handle:
            self.assertEqual(handle.read(), "# old copy\n")

    def test_refuses_a_version_that_is_not_newer(self):
        stale = BUNDLE_TEXT.replace('"9.9.9"', '"%s"' % config.VERSION)
        answer = self._apply(stale)
        self.assertFalse(answer["ok"])
        self.assertIn("not newer", answer["error"])

    def test_refuses_a_download_that_does_not_parse(self):
        answer = self._apply(BUNDLE_TEXT + "\ndef broken(:\n")
        self.assertFalse(answer["ok"])
        with open(self.target) as handle:
            self.assertEqual(handle.read(), "# old copy\n")


class TestNoteIfUpdated(unittest.TestCase):
    def setUp(self):
        from procwatch import db
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def _run_as(self, version, now):
        with mock.patch.object(selfupdate.config, "VERSION", version):
            return selfupdate.note_if_updated(self.conn, now=now)

    def test_the_first_install_is_not_an_update(self):
        self.assertIsNone(self._run_as("1.0.0", now=1000))

    def test_the_same_version_stays_quiet(self):
        self._run_as("1.0.0", now=1000)
        self.assertIsNone(self._run_as("1.0.0", now=2000))

    def test_a_new_version_is_news_exactly_once(self):
        self._run_as("1.0.0", now=1000)
        first = self._run_as("1.1.0", now=2000)
        self.assertEqual(first, {"from": "1.0.0", "to": "1.1.0", "ts": 2000})
        self.assertIsNone(self._run_as("1.1.0", now=3000))

    def test_the_timeline_shows_the_update(self):
        from procwatch import events
        self._run_as("1.0.0", now=1000)
        self._run_as("1.1.0", now=2000)
        rows = [r for r in events._from_database(self.conn, since=0)
                if r["kind"] == "procwatch-update"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject"], "Procwatch")
        self.assertEqual(rows[0]["detail"], "1.0.0 to 1.1.0")
        self.assertEqual(rows[0]["ts"], 2000)
        # And the kind reads as a sentence the timeline can print.
        self.assertIn("procwatch-update", events.MEANINGS)

    def test_a_downgrade_to_a_seen_version_is_not_reannounced(self):
        self._run_as("1.0.0", now=1000)
        self._run_as("1.1.0", now=2000)
        self.assertIsNone(self._run_as("1.0.0", now=3000))

    def test_a_database_older_than_the_table_inherits_its_past(self):
        # versions.py recorded what Procwatch.app declared before this table
        # existed; the first run of a new version is still an update.
        from procwatch import versions
        versions.init(self.conn)
        with self.conn:
            self.conn.execute(
                "INSERT INTO app_version (app, version, first_ts, last_ts) "
                "VALUES (?,?,?,?)", ("Procwatch", "1.0.0", 500, 900))
        first = self._run_as("1.1.0", now=2000)
        self.assertEqual(first, {"from": "1.0.0", "to": "1.1.0", "ts": 2000})

    def test_the_timeline_says_it_once_even_with_the_app_scanned(self):
        # versions.py records Procwatch.app like any other application, and
        # the timeline used to report the same update from both tables.
        from procwatch import events, versions
        versions.init(self.conn)
        with self.conn:
            self.conn.executemany(
                "INSERT INTO app_version (app, version, first_ts, last_ts) "
                "VALUES (?,?,?,?)",
                [("Procwatch", "1.0.0", 500, 900),
                 ("Procwatch", "1.1.0", 2100, 2100)])
        self._run_as("1.0.0", now=600)
        self._run_as("1.1.0", now=2000)
        rows = [r for r in events._from_database(self.conn, since=0)
                if "update" in r["kind"] and r["subject"] == "Procwatch"]
        self.assertEqual([r["kind"] for r in rows], ["procwatch-update"])

    def test_an_inherited_past_matching_the_present_stays_quiet(self):
        from procwatch import versions
        versions.init(self.conn)
        with self.conn:
            self.conn.execute(
                "INSERT INTO app_version (app, version, first_ts, last_ts) "
                "VALUES (?,?,?,?)", ("Procwatch", "1.1.0", 500, 900))
        self.assertIsNone(self._run_as("1.1.0", now=2000))


class TestApplyGit(unittest.TestCase):
    def setUp(self):
        _reset_cache()
        self.addCleanup(_reset_cache)

    def test_git_failure_surfaces_gits_own_words(self):
        failed = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="fatal: not possible to fast-forward")
        with mock.patch.object(selfupdate, "mode", return_value="git"), \
                mock.patch.object(selfupdate.subprocess, "run",
                                  return_value=failed):
            answer = selfupdate.apply()
        self.assertFalse(answer["ok"])
        self.assertIn("fast-forward", answer["error"])

    def test_git_success_reads_the_new_version(self):
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(selfupdate, "mode", return_value="git"), \
                mock.patch.object(selfupdate.subprocess, "run",
                                  return_value=done) as run:
            answer = selfupdate.apply()
        self.assertTrue(answer["ok"], answer["error"])
        self.assertTrue(answer["restart"])
        # fetch then ff-only merge, nothing else.
        steps = [call.args[0][:5] for call in run.call_args_list]
        self.assertEqual(len(steps), 2)
        self.assertIn("fetch", steps[0])
        self.assertIn("merge", steps[1])

    def test_neither_shape_refuses_plainly(self):
        with mock.patch.object(selfupdate, "mode", return_value="none"):
            answer = selfupdate.apply()
        self.assertFalse(answer["ok"])
        self.assertIn("neither", answer["error"])


if __name__ == "__main__":
    unittest.main()
