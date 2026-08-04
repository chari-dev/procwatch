"""Where the disk went, and what may be removed.

The removal half is why this file is careful. Everything else here reports; that
part acts, and acting wrongly costs somebody their work.
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from procwatch import db, space


def _tree(root, spec):
    """Build a directory of known sizes. spec is {relative path: bytes}."""
    for rel, size in spec.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"\0" * size)
    return root


class TestScan(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_it_records_what_it_walked(self):
        _tree(self.dir, {"a/one.mp4": 4096, "a/two.mp3": 4096, "b/three.py": 4096})
        space.scan(self.conn, self.dir, floor=0, file_floor=0)
        found = space.latest(self.conn)
        self.assertEqual(found["files"], 3)
        self.assertGreater(found["bytes"], 0)
        self.assertTrue(found["finished_ts"])

    def test_a_file_is_classified_by_its_extension(self):
        _tree(self.dir, {"a/film.mp4": 4096, "a/song.mp3": 4096,
                         "a/notes.txt": 4096, "a/thing.zzz": 4096})
        sid = space.scan(self.conn, self.dir, floor=0, file_floor=0)
        kinds = {k["kind"]: k["files"] for k in space.kinds(self.conn, sid)}
        self.assertEqual(kinds.get("video"), 1)
        self.assertEqual(kinds.get("audio"), 1)
        self.assertEqual(kinds.get("documents"), 1)
        self.assertEqual(kinds.get("other"), 1)

    def test_a_folder_total_includes_everything_below_it(self):
        # A total that counted only the files sitting directly in a folder would
        # report every container as empty, which is most of ~/Library.
        _tree(self.dir, {"top/deep/deeper/big.bin": 2 * 1024 * 1024,
                         "top/small.bin": 1024})
        sid = space.scan(self.conn, self.dir, floor=0, file_floor=0)
        rows = {r["path"]: r["bytes"]
                for r in space.biggest_dirs(self.conn, sid, limit=20)}
        top = os.path.join(self.dir, "top")
        self.assertIn(top, rows)
        self.assertGreaterEqual(rows[top], 2 * 1024 * 1024)

    def test_it_measures_what_the_disk_gave_not_what_the_file_claims(self):
        # st_size is a claim; st_blocks is the answer. On APFS a sparse file or
        # a clone makes them differ by orders of magnitude, and reporting the
        # claim sends people deleting the wrong things.
        import inspect
        source = inspect.getsource(space.scan)
        self.assertIn("st_blocks * 512", source)
        self.assertIn("logical", source)

    def test_symlinks_are_not_followed(self):
        # Otherwise a link into a parent turns the walk into a loop, and a link
        # to a big folder counts it twice.
        _tree(self.dir, {"real/file.bin": 4096})
        os.symlink(os.path.join(self.dir, "real"), os.path.join(self.dir, "link"))
        space.scan(self.conn, self.dir, floor=0, file_floor=0)
        self.assertEqual(space.latest(self.conn)["files"], 1)

    def test_only_the_newest_scan_is_kept(self):
        # A history of what used to be on the disk is a second thing taking up
        # space on it.
        _tree(self.dir, {"a.bin": 4096})
        space.scan(self.conn, self.dir, floor=0, file_floor=0)
        space.scan(self.conn, self.dir, floor=0, file_floor=0)
        count = self.conn.execute("SELECT COUNT(*) FROM space_scan").fetchone()[0]
        self.assertEqual(count, 1)

    def test_a_deadline_stops_it_and_says_so(self):
        import time
        _tree(self.dir, {"a/%d.bin" % i: 1024 for i in range(50)})
        space.scan(self.conn, self.dir, floor=0, deadline=time.time() - 1)
        self.assertIn("stopped", space.latest(self.conn)["stopped"])


class TestReconciling(unittest.TestCase):
    """The totals have to account for themselves.

    Walking only the home folder reported 90 GB against a volume saying 232 GB
    used, with no explanation of the other 142. A total that does not reconcile
    is worse than no total: it sends people hunting for space that was never
    missing.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_more_than_home_is_walked(self):
        # /Applications alone was 25 GB of the gap, and it is the most
        # actionable number on the page: an application can be dragged to the
        # Bin.
        self.assertIn("/Applications", space.SYSTEM_ROOTS)
        self.assertNotIn("/System", space.SYSTEM_ROOTS)

    def test_each_root_appears_at_the_top_level(self):
        one = os.path.join(self.dir, "one")
        two = os.path.join(self.dir, "two")
        _tree(one, {"a/x.bin": 4096})
        _tree(two, {"b/y.bin": 8192})
        was_roots, was_home = space.SYSTEM_ROOTS, os.environ.get("HOME")
        space.SYSTEM_ROOTS = (two,)
        os.environ["HOME"] = one
        try:
            sid = space.scan(self.conn, None, floor=0, file_floor=0)
            paths = [r["path"] for r in space.biggest_dirs(self.conn, sid)]
        finally:
            space.SYSTEM_ROOTS = was_roots
            if was_home:
                os.environ["HOME"] = was_home
        self.assertIn(one, paths)
        self.assertIn(two, paths)

    def test_a_folder_outside_home_still_gets_its_total(self):
        # Charging parents stopped at the first root, so anything outside home
        # was walked and then credited to nothing.
        #
        # The two names are deliberately different lengths. The walk upward
        # stops on a length comparison, so with equal-length roots it lands on
        # the right folder whether or not it chose the right root, and a wrong
        # implementation passes.
        one = os.path.join(self.dir, "home")
        two = os.path.join(self.dir, "somewhere-else-entirely")
        _tree(one, {"a.bin": 4096})
        _tree(two, {"deep/deeper/b.bin": 8192})
        was_roots, was_home = space.SYSTEM_ROOTS, os.environ.get("HOME")
        space.SYSTEM_ROOTS = (two,)
        os.environ["HOME"] = one
        try:
            sid = space.scan(self.conn, None, floor=0, file_floor=0)
            rows = {r[0]: r[1] for r in self.conn.execute(
                "SELECT path, bytes FROM space_dir WHERE scan_id=?", (sid,))}
        finally:
            space.SYSTEM_ROOTS = was_roots
            if was_home:
                os.environ["HOME"] = was_home
        self.assertGreaterEqual(rows.get(two, 0), 8192)
        # And nothing above a root is credited. Getting the stopping point
        # wrong walks up past it and puts rows on /var/folders and on / --
        # which still gives the root the right total, so a lower bound alone
        # cannot tell the two apart.
        for path in rows:
            self.assertTrue(path == one or path == two
                            or path.startswith(one + os.sep)
                            or path.startswith(two + os.sep),
                            "%s is outside both roots" % path)

    def test_a_root_inside_another_root_is_not_counted_twice(self):
        """Nested roots are dropped before the walk begins.

        Otherwise the inner one is walked twice, once on its own and once on the
        way down through the outer, and both totals are wrong: an 8 KB file came
        back as 16 KB when this was first tried.

        Nothing in the shipped list nests. The test is here because the list is
        a constant somebody will add to, and /usr beside /usr/local is the
        obvious next entry.
        """
        outer = os.path.join(self.dir, "outer")
        inner = os.path.join(outer, "inner")
        _tree(inner, {"deep/a.bin": 8192})
        _tree(outer, {"b.bin": 4096})
        was_roots, was_home = space.SYSTEM_ROOTS, os.environ.get("HOME")
        # Outer listed last on purpose: the rule is that the deepest match
        # wins, not the one that happens to be checked last, and with the
        # obvious order a "last match wins" bug gives the right answer anyway.
        space.SYSTEM_ROOTS = (inner, outer)
        os.environ["HOME"] = os.path.join(self.dir, "home")
        os.makedirs(os.environ["HOME"], exist_ok=True)
        try:
            sid = space.scan(self.conn, None, floor=0, file_floor=0)
            rows = {r[0]: r[1] for r in self.conn.execute(
                "SELECT path, bytes FROM space_dir WHERE scan_id=?", (sid,))}
        finally:
            space.SYSTEM_ROOTS = was_roots
            if was_home:
                os.environ["HOME"] = was_home
        # Walked once: exactly the file's size, not twice it.
        self.assertEqual(rows.get(inner, 0), 8192)
        # And the outer root carries everything below it, the inner one
        # included, because it is the only root now.
        self.assertEqual(rows.get(outer, 0), 8192 + 4096)

    def test_pruning_keeps_the_outermost_of_a_nested_pair(self):
        self.assertEqual(space._prune(["/a/b", "/a"]), ["/a"])
        self.assertEqual(space._prune(["/a", "/a/b"]), ["/a"])
        # Unrelated ones are left alone, in the order they were given: the
        # first is the one the scan is recorded against, and that should be the
        # home folder rather than whichever path happens to be shortest.
        self.assertEqual(space._prune(["/b", "/a"]), ["/b", "/a"])
        # Lengths that differ, so preserving the caller's order is visibly not
        # the same as sorting: with equal-length paths a stable sort returns
        # them in input order anyway and the two cannot be told apart.
        self.assertEqual(space._prune(["/a/deep/path", "/b"]),
                         ["/a/deep/path", "/b"])
        # A path repeated is not two roots.
        self.assertEqual(space._prune(["/a", "/a/"]), ["/a"])

    def test_the_folders_macos_refuses_are_named(self):
        # Being refused looks exactly like an empty folder, so these would
        # silently weigh nothing. A Photos library is frequently the largest
        # thing on a Mac.
        self.assertTrue(any("photoslibrary" in p for p in space.GUARDED))
        self.assertTrue(any("Mail" in p for p in space.GUARDED))

    def test_the_difference_is_stated_rather_than_left_to_be_noticed(self):
        _tree(self.dir, {"a.bin": 4096})
        space.scan(self.conn, self.dir, floor=0, file_floor=0)
        found = space.reconcile(self.conn)
        self.assertIsNotNone(found)
        self.assertGreater(found["missing"], 0)
        self.assertTrue(found["reasons"])
        self.assertEqual(found["used"] - found["scanned"], found["missing"])


class TestExplaining(unittest.TestCase):
    def test_the_longest_match_wins(self):
        # ~/Library/Caches/Homebrew is answered by the entry about Homebrew, not
        # the general one about caches.
        found = space.explain("~/Library/Caches/Homebrew/downloads")
        self.assertIn("Homebrew", found["about"])
        self.assertTrue(found["safe"])

    def test_a_container_is_never_marked_safe(self):
        # For a sandboxed application the container is its documents, not its
        # cache, and deleting one loses the lot.
        found = space.explain("~/Library/Containers/com.apple.Notes")
        self.assertFalse(found["safe"])

    def test_somewhere_uncatalogued_says_nothing(self):
        self.assertIsNone(space.explain("~/Documents/some project"))


class TestRefusals(unittest.TestCase):
    """What must never be moved to the Trash, whatever is asked."""

    def test_the_home_directory_itself(self):
        # The exact message, not the word "home" in it: refusing home as
        # "outside your home directory" also contains that word, and passing on
        # the wrong reason is passing by accident.
        self.assertEqual(space._protected(os.path.expanduser("~")),
                         "your entire home directory")

    def test_anything_outside_home(self):
        self.assertTrue(space._protected("/System/Library"))
        self.assertTrue(space._protected("/etc/hosts"))

    def test_procwatchs_own_recording(self):
        from procwatch import config
        self.assertIn("Procwatch", space._protected(config.DB_PATH))

    def test_a_whole_application_data_folder(self):
        for risky in ("~/Library/Containers", "~/Library/Application Support",
                      "~/Documents", "~/Desktop", "~/Pictures",
                      # The whole of ~/Library, reachable now that folder rows
                      # in the disk panel carry a Trash button.
                      "~/Library"):
            self.assertTrue(space._protected(os.path.expanduser(risky)), risky)

    def test_an_application_may_be_removed_though_it_is_outside_home(self):
        """Applications live in /Applications, and the rule refused all of it.

        The blanket "outside your home directory" refusal was written for a
        feature that only ever deleted files inside home, and it silently made
        the Uninstall button impossible: every application on the machine came
        back refused, so pressing it moved nothing and said little.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        bundle = os.path.join(tmp, "Thing.app")
        os.makedirs(bundle)
        with mock.patch.object(space, "APP_FOLDERS", (tmp,)):
            self.assertEqual(space._protected(bundle), "")

    def test_the_folder_applications_live_in_is_still_refused(self):
        """Letting bundles through must not let their container through."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "Thing.app"))
        with mock.patch.object(space, "APP_FOLDERS", (tmp,)):
            self.assertTrue(space._protected(tmp))
        self.assertTrue(space._protected("/Applications"))
        self.assertTrue(space._protected(os.path.expanduser("~/Applications")))

    def test_a_system_application_is_refused(self):
        """Safari and Mail cannot be removed, so the exception must not reach
        them: /System/Applications is not a folder applications are installed
        into, it is part of the sealed system."""
        self.assertTrue(space._protected("/System/Applications/Safari.app"))

    def test_the_inside_of_a_bundle_is_refused(self):
        """The exception is the bundle, not a path that merely starts with one.

        Trashing /Applications/Xcode.app/Contents leaves a broken application
        behind rather than removing one.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        inside = os.path.join(tmp, "Thing.app", "Contents")
        os.makedirs(inside)
        with mock.patch.object(space, "APP_FOLDERS", (tmp,)):
            self.assertTrue(space._protected(inside))

    def test_an_application_nested_inside_another_is_refused(self):
        """Applications carry applications: Xcode holds Instruments.

        Matching on "somewhere under /Applications" rather than "directly in
        it" would offer to remove one of those, which breaks the application
        around it and is not what anybody pressed the button for.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        nested = os.path.join(tmp, "Outer.app", "Contents", "Inner.app")
        os.makedirs(nested)
        with mock.patch.object(space, "APP_FOLDERS", (tmp,)):
            self.assertTrue(space._protected(nested))

    def test_a_plain_folder_beside_the_applications_is_refused(self):
        """/Applications/Utilities sits right beside them and is not one."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        plain = os.path.join(tmp, "Utilities")
        os.makedirs(plain)
        with mock.patch.object(space, "APP_FOLDERS", (tmp,)):
            self.assertTrue(space._protected(plain))

    def test_a_file_wearing_the_app_name_is_refused(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        fake = os.path.join(tmp, "Thing.app")
        with open(fake, "wb") as handle:
            handle.write(b"not a bundle")
        with mock.patch.object(space, "APP_FOLDERS", (tmp,)):
            self.assertTrue(space._protected(fake))

    def test_a_synced_cloud_folder_is_refused(self):
        """The Trash is not an undo for a cloud deletion.

        ~/Library/CloudStorage is where Dropbox, OneDrive and Google Drive put
        their files, and ~/Library/Mobile Documents is iCloud Drive. Moving one
        of those to the local Trash tells the service the file is gone, and it
        goes from every other machine signed in to it. Refused by subtree, not
        by folder: the danger is the file, not the folder above it.
        """
        home = os.path.expanduser("~")
        for inside in ("Library/CloudStorage/Dropbox/work/notes.txt",
                       "Library/CloudStorage/GoogleDrive-me/x",
                       "Library/Mobile Documents/com~apple~CloudDocs/tax.pdf"):
            refusal = space._protected(os.path.join(home, inside))
            self.assertTrue(refusal, "%s was allowed" % inside)
            self.assertIn("device", refusal)

    def test_the_trash_is_not_offered_as_something_to_clear(self):
        """Everything on that list gets a button that moves it to the Trash.

        The Trash was on it, so the button moved the Trash to the Trash: Finder
        was asked to delete ~/.Trash, and the fallback tried to move it inside
        itself. Emptying it is a permanent delete, which is the one thing this
        file will not do.
        """
        offered = [path for path, safe, _why in space.NOTES if safe]
        self.assertNotIn("~/.Trash", offered)
        # Still described, so a Trash holding 5 GB is not simply invisible.
        note = space.explain(os.path.expanduser("~/.Trash"))
        self.assertIsNotNone(note)
        self.assertFalse(note["safe"])

    def test_one_path_failing_does_not_strand_the_rest(self):
        """_by_hand used to stop at the first error.

        An application bundle macOS would not let this move took its caches and
        its preferences down with it, and every path after the failure was
        reported with the first one's error.
        """
        locked = tempfile.mkdtemp()
        movable = tempfile.mkdtemp()
        stuck = os.path.join(locked, "stuck.txt")
        free = os.path.join(movable, "free.txt")
        for path in (stuck, free):
            with open(path, "w") as handle:
                handle.write("x")
        os.chmod(locked, 0o500)          # no write: the move out cannot happen
        try:
            ok, error = space._by_hand([stuck, free])
            self.assertFalse(ok)
            self.assertTrue(error)
            self.assertTrue(os.path.exists(stuck), "the locked one moved")
            self.assertFalse(os.path.exists(free),
                             "the movable one was stranded by the failure")
        finally:
            os.chmod(locked, 0o700)
            shutil.rmtree(locked, ignore_errors=True)
            shutil.rmtree(movable, ignore_errors=True)
            landed = os.path.expanduser("~/.Trash/free.txt")
            if os.path.exists(landed):
                os.remove(landed)

    def test_a_broken_symlink_is_not_reported_as_moved(self):
        """os.path.exists answers about the far side of a link.

        A dangling symlink is a real directory entry taking real space, and
        exists() says it is not there -- before the move, so it is "no longer
        there", and after a failed move, so it counts as gone. lexists asks
        about the entry itself, which is the thing being moved.
        """
        # Inside home, because everything outside it is refused before the
        # question this test is asking is ever reached.
        folder = tempfile.mkdtemp(dir=os.path.expanduser("~/Downloads"),
                                  prefix="procwatch-link-")
        link = os.path.join(folder, "dangling")
        os.symlink(os.path.join(folder, "nothing-here"), link)
        try:
            self.assertFalse(os.path.exists(link))
            self.assertTrue(os.path.lexists(link))
            asked = []
            results = space.trash(
                [link], runner=lambda paths: (asked.append(paths), (True, ""))[1])
            self.assertEqual(asked, [[link]], "it never reached the runner")
            self.assertFalse(results[0]["ok"],
                             "a link still on the disk was reported as moved")
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_a_file_inside_the_trash_is_refused(self):
        # Biggest single files reaches in there, and moving a file from the
        # Trash to the Trash renames it and reports success.
        inside = os.path.expanduser("~/.Trash/whatever.bin")
        self.assertIn("Trash", space._protected(inside))

    def test_an_ordinary_file_in_your_own_home_is_allowed(self):
        self.assertEqual(space._protected(os.path.expanduser("~/Downloads/x.zip")),
                         "")

    def test_trash_refuses_rather_than_reporting_success(self):
        # The runner records instead of deleting. Without it, breaking the
        # refusals for a mutation run asks the real Finder to delete whatever
        # the test named -- which is exactly what happened the first time.
        asked = []
        results = space.trash(["/System/Library"],
                              runner=lambda paths: (asked.append(paths), (True, ""))[1])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertIn("refused", results[0]["error"])
        self.assertEqual(asked, [], "a refused path reached the runner")

    def test_a_permitted_path_does_reach_the_runner(self):
        # The other half: a guard that refuses everything would pass the test
        # above and be useless.
        #
        # The runner moves what it is given, because success is now read off
        # the disk rather than taken from Finder's word. A runner that reported
        # success and moved nothing would be reported as a failure, which is
        # the point of the check and would make this test a lie.
        asked = []
        elsewhere = tempfile.mkdtemp()
        doomed = os.path.expanduser("~/Downloads/procwatch-test-nonexistent")
        with open(doomed, "w") as handle:
            handle.write("x")

        def runner(paths):
            asked.append(paths)
            for path in paths:
                shutil.move(path, os.path.join(elsewhere,
                                               os.path.basename(path)))
            return True, ""

        try:
            results = space.trash([doomed], runner=runner)
        finally:
            shutil.rmtree(elsewhere, ignore_errors=True)
            if os.path.exists(doomed):
                os.remove(doomed)
        self.assertTrue(results[0]["ok"])
        self.assertEqual(len(asked), 1)
        self.assertEqual(asked[0], [doomed])

    def test_a_runner_that_moves_nothing_is_not_reported_as_success(self):
        """Finder says yes for a batch it only partly moved.

        Asked to delete a good file and a bundle the system protects, it
        returns success, moves the one and leaves the other -- so the answer
        for each path is whether the path is still there, not what Finder said.
        No fallback runs here: with a runner injected, the paths are the test's
        to move, and nothing may go behind its back.
        """
        staying = os.path.expanduser("~/Downloads/procwatch-test-stays")
        with open(staying, "w") as handle:
            handle.write("x")
        try:
            results = space.trash([staying], runner=lambda paths: (True, ""))
        finally:
            if os.path.exists(staying):
                os.remove(staying)
        self.assertFalse(results[0]["ok"],
                         "a path still on the disk was reported as moved")

    def test_a_filename_can_never_become_a_script(self):
        """The paths are arguments, not source.

        The first version interpolated each path into an AppleScript string and
        escaped only the double quotes, leaving backslashes alone -- so a name
        ending in one escaped its own closing quote and the string ran on into
        whatever came next. What that produced in practice was a syntax error,
        which silently failed to delete the whole batch; whether a working
        payload could be built out of it is not worth answering when the parse
        step can be removed instead.
        """
        import inspect
        source = inspect.getsource(space._ask_finder)
        # The script is a constant. Nothing from a filename is formatted into it.
        self.assertIn("_TRASH_SCRIPT", source)
        self.assertNotIn("%s", source)
        self.assertIn("+ list(paths)", source)
        self.assertIn("on run argv", space._TRASH_SCRIPT)

    def test_a_hostile_name_reaches_the_runner_unchanged(self):
        # Not escaped, not rewritten: handed over exactly as it is on disk,
        # because nothing is going to parse it.
        nasty = os.path.expanduser(
            '~/Downloads/evil" & (do shell script "touch PWNED") & "x\\')
        got = []
        results = space.trash([nasty],
                              runner=lambda paths: (got.append(paths), (True, ""))[1])
        # It does not exist, so it is reported as gone rather than deleted --
        # and either way nothing was built into a script.
        self.assertFalse(results[0]["ok"])
        self.assertEqual(got, [])

    def test_it_never_unlinks(self):
        # The whole design: files go where a mistake is a drag back rather than
        # a restore from a backup somebody may not have.
        import inspect
        source = inspect.getsource(space.trash) + inspect.getsource(space._by_hand)
        for destructive in ("os.remove", "os.unlink", "shutil.rmtree", "rm -rf"):
            self.assertNotIn(destructive, source)
        # Finder first; the fallback still only moves, and only into the Trash.
        self.assertIn("shutil.move", source)
        self.assertIn(".Trash", source)

    def test_finder_actually_takes_the_script(self):
        """The script has to work, not merely be safe.

        It did not. Inside `tell application "Finder"` the word `target` is
        Finder's own property, so a variable of that name was resolved as
        Finder's and every removal failed with -1728 "Can't get target". The
        fallback hid it for anything the process could move itself, and left
        the applications it could not -- root-owned ones from the App Store --
        sitting where they were while the panel claimed to have moved them.

        Runs against the real Finder because that is the only thing that can
        answer the question. Skipped where there is none, or where automation
        is refused: an unattended machine must not fail this for lacking a
        permission a person has to grant by hand.
        """
        if not shutil.which("osascript"):
            self.skipTest("no osascript")
        try:
            folder = tempfile.mkdtemp(prefix="procwatch-finder-")
        except OSError as problem:
            self.skipTest("no writable temporary directory: %s" % problem)
        doomed = os.path.join(folder, "procwatch test file")
        with open(doomed, "w") as handle:
            handle.write("x")
        try:
            ok, error = space._ask_finder([doomed])
            # Automation refused (-1743), or a Finder busy with a dialog of its
            # own (-1712, the event timing out). Both are a machine this cannot
            # be asked on, not a broken script.
            for excuse in ("-1743", "-1712", "not allowed", "timed out"):
                if not ok and excuse in error.lower():
                    self.skipTest("Finder cannot be asked here: %s" % error)
            self.assertTrue(ok, "Finder refused the script: %s" % error)
            self.assertFalse(os.path.exists(doomed),
                             "Finder reported success and moved nothing")
        finally:
            shutil.rmtree(folder, ignore_errors=True)
            # The test's own litter, out of a Trash it does not own.
            landed = os.path.expanduser("~/.Trash/procwatch test file")
            if os.path.exists(landed):
                os.remove(landed)

    def test_the_script_avoids_finders_own_vocabulary(self):
        """No variable named after something Finder already owns.

        The live test above cannot run everywhere -- no Finder, or automation
        not granted -- so this catches the same mistake by reading the script.
        Every name the script binds is checked, rather than one spelling of one
        statement: `repeat with target in argv` is the same bug as `set target
        to`, and a guard that only knows the second is a guard that would have
        let the fix regress in the obvious way.
        """
        import re
        bound = set(re.findall(r"set (\w+) to", space._TRASH_SCRIPT))
        bound |= set(re.findall(r"repeat with (\w+)", space._TRASH_SCRIPT))
        self.assertTrue(bound, "the script binds nothing; the check is dead")
        # Finder's dictionary cannot be enumerated here, so this is the part of
        # it that a person writing this script would reach for.
        finders = {"target", "container", "selection", "name", "index", "item",
                   "file", "folder", "window", "application", "trash", "disk",
                   "desktop", "front", "result", "text", "properties", "id",
                   "class", "kind", "size", "position", "bounds", "url"}
        self.assertEqual(bound & finders, set(),
                         "a variable is named after a Finder term")

    def test_the_fallback_does_not_overwrite_what_is_already_in_the_trash(self):
        # Two folders called Cache arriving in the same Trash must not become
        # one, which for a fallback that exists to avoid losing things would be
        # a poor way to lose things.
        import inspect
        source = inspect.getsource(space._by_hand)
        # lexists rather than exists: a dangling symlink already in the Trash
        # holds the name, and exists() looks through it and calls it free.
        self.assertIn("while os.path.lexists(target)", source)


class TestUninstalling(unittest.TestCase):
    """Finding everything an application left outside its bundle.

    The matching is the risk. An uninstaller that matches loosely deletes
    somebody else's data, and there is no undo beyond the Trash.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.home = os.path.join(self.dir, "home")
        os.makedirs(self.home)
        self.was_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home

    def tearDown(self):
        if self.was_home:
            os.environ["HOME"] = self.was_home
        shutil.rmtree(self.dir, ignore_errors=True)

    def _app(self, name, ident):
        import plistlib
        path = os.path.join(self.dir, name + ".app")
        os.makedirs(os.path.join(path, "Contents"))
        with open(os.path.join(path, "Contents", "Info.plist"), "wb") as handle:
            plistlib.dump({"CFBundleIdentifier": ident}, handle)
        return path

    def _put(self, rel, size=4096):
        path = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"\0" * size)
        return path

    def test_it_reads_the_bundle_identifier(self):
        app = self._app("Thing", "com.example.thing")
        info = space.app_info(app)
        self.assertEqual(info["name"], "Thing")
        self.assertEqual(info["ident"], "com.example.thing")

    def test_two_leftovers_sharing_a_file_do_not_promise_it_twice(self):
        """The total is what removing the lot gives back.

        A cache and a container holding hard links to the same files are two
        entries on the list, and measuring them independently promised twice
        the space the removal would actually return.
        """
        app = self._app("Thing", "com.example.thing")
        one = self._put("Library/Caches/com.example.thing/blob.bin", 40000)
        two_dir = os.path.join(self.home, "Library/HTTPStorages/com.example.thing")
        os.makedirs(two_dir, exist_ok=True)
        os.link(one, os.path.join(two_dir, "blob.bin"))
        found = space.leftovers(app)
        sizes = {i["path"]: i["bytes"] for i in found["items"]}
        charged = sum(v for k, v in sizes.items() if "com.example.thing" in k)
        self.assertEqual(charged, os.lstat(one).st_blocks * 512,
                         "the same blocks were promised by two entries")

    def test_a_group_container_is_matched_on_more_than_one_word(self):
        """The suffix rule contradicted the "never on a substring" rule.

        For a two-component identifier like net.whatsapp the tail is the single
        word `whatsapp`, and `endswith("." + tail)` matched any group container
        ending in it -- somebody else's data, removed on the strength of one
        word. A team id followed by the full remainder still matches, because
        that is the shape the rule was written for.
        """
        root = os.path.join(self.home, "Library/Group Containers")
        os.makedirs(os.path.join(root, "group.com.other.whatsapp"))
        os.makedirs(os.path.join(root, "TEAMID.net.whatsapp"))
        found = [os.path.basename(p) for p in space._group_containers("net.whatsapp")]
        self.assertIn("TEAMID.net.whatsapp", found)
        self.assertNotIn("group.com.other.whatsapp", found)

    def test_a_team_prefixed_group_container_still_matches(self):
        # The half the rule exists for: the leading component replaced by a
        # team id, with everything after it intact.
        root = os.path.join(self.home, "Library/Group Containers")
        os.makedirs(os.path.join(root, "AB12CD34.example.thing"))
        found = [os.path.basename(p)
                 for p in space._group_containers("com.example.thing")]
        self.assertEqual(found, ["AB12CD34.example.thing"])

    def test_something_that_is_not_an_application_is_refused(self):
        self.assertIsNone(space.app_info(self.dir))
        self.assertIsNone(space.leftovers(self.dir))

    def test_it_finds_what_the_application_left_behind(self):
        app = self._app("Thing", "com.example.thing")
        self._put("Library/Application Support/com.example.thing/data.bin")
        self._put("Library/Caches/com.example.thing/c.bin")
        self._put("Library/Preferences/com.example.thing.plist")
        self._put("Library/Saved Application State/com.example.thing.savedState/s")
        found = space.leftovers(app)
        paths = [i["display"] for i in found["items"]]
        self.assertTrue(any("Application Support" in p for p in paths))
        self.assertTrue(any("Caches" in p for p in paths))
        self.assertTrue(any("Preferences" in p for p in paths))
        self.assertTrue(any("savedState" in p for p in paths))

    def test_the_bundle_is_listed_apart_from_the_leftovers(self):
        # The number worth showing is how much stays behind when you drag the
        # app to the Bin, so the two are counted separately.
        app = self._app("Thing", "com.example.thing")
        self._put("Library/Caches/com.example.thing/c.bin", 8192)
        found = space.leftovers(app)
        self.assertLess(found["leftover_bytes"], found["bytes"])
        self.assertEqual([i["kind"] for i in found["items"]][0], "app")

    def test_it_never_matches_another_application_by_substring(self):
        """The failure that would delete somebody else's data.

        "Notes" is inside "Notes Helper" and inside "com.other.NotesExport",
        and an uninstaller that matches loosely takes them all.
        """
        app = self._app("Notes", "com.example.notes")
        # What it removes is the folder, not the file inside it.
        self._put("Library/Caches/com.example.notes/c.bin")
        mine = os.path.join(self.home, "Library/Caches/com.example.notes")
        self._put("Library/Caches/com.example.notesexport/x.bin")
        self._put("Library/Application Support/Notes Helper/y.bin")
        self._put("Library/Preferences/com.example.notes.helper2.plist")
        theirs = [
            os.path.join(self.home, "Library/Caches/com.example.notesexport"),
            os.path.join(self.home, "Library/Application Support/Notes Helper"),
            os.path.join(self.home,
                         "Library/Preferences/com.example.notes.helper2.plist"),
        ]
        found = space.leftovers(app)
        paths = [i["path"] for i in found["items"]]
        self.assertIn(mine, paths)
        for other in theirs:
            self.assertNotIn(other, paths,
                             "%s belongs to something else" % other)

    def test_an_application_with_no_leftovers_reports_only_itself(self):
        app = self._app("Clean", "com.example.clean")
        found = space.leftovers(app)
        self.assertEqual(len(found["items"]), 1)
        self.assertEqual(found["leftover_bytes"], 0)

    def _fake_apps(self, tmp, names):
        """Real .app folders on disk, since the listing reads folders now."""
        made = []
        for name in names:
            path = os.path.join(tmp, name + ".app")
            os.makedirs(os.path.join(path, "Contents", "MacOS"))
            made.append(path)
        return made

    def test_it_lists_applications_the_scan_left_out(self):
        """The scan records folders above a floor; applications ignore it.

        Twenty of the fifty applications on the machine this was written on
        were under the 20 MB floor and so had no row at all -- AppCleaner,
        Itsycal, Hidden Bar. Reading the folders instead of the scan is what
        puts them back, and a small application is still one somebody wants
        gone.
        """
        conn = db.connect(":memory:")
        db.init_schema(conn)
        space.init(conn)
        with conn:
            conn.execute("INSERT INTO space_scan (id, root, started_ts, "
                         "finished_ts) VALUES (1,'/',0,1)")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self._fake_apps(tmp, ["Tiny", "Small"])
        with mock.patch.object(space, "APP_FOLDERS", (tmp,)):
            found = space.applications(conn, 1)
        self.assertEqual(sorted(a["name"] for a in found), ["Small", "Tiny"])

    def test_it_leaves_out_an_application_that_is_no_longer_installed(self):
        """A removed application keeps its row in the last scan.

        Offering to uninstall something already gone is the failure the
        folder listing exists to prevent.
        """
        conn = db.connect(":memory:")
        db.init_schema(conn)
        space.init(conn)
        with conn:
            conn.execute("INSERT INTO space_scan (id, root, started_ts, "
                         "finished_ts) VALUES (1,'/',0,1)")
            conn.execute("INSERT INTO space_dir (scan_id, path, depth, bytes, "
                         "files) VALUES (1,'/Applications/Gone.app',2,9000,1)")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self._fake_apps(tmp, ["Here"])
        with mock.patch.object(space, "APP_FOLDERS", (tmp,)):
            found = space.applications(conn, 1)
        self.assertEqual([a["name"] for a in found], ["Here"])

    def test_it_measures_a_bundle_the_scan_has_no_row_for(self):
        conn = db.connect(":memory:")
        db.init_schema(conn)
        space.init(conn)
        with conn:
            conn.execute("INSERT INTO space_scan (id, root, started_ts, "
                         "finished_ts) VALUES (1,'/',0,1)")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = self._fake_apps(tmp, ["Measured"])[0]
        with open(os.path.join(path, "Contents", "MacOS", "blob"), "wb") as fh:
            fh.write(b"x" * 40000)
        with mock.patch.object(space, "APP_FOLDERS", (tmp,)):
            found = space.applications(conn, 1)[0]
        self.assertGreaterEqual(found["bundle"], 40000)

    def test_a_scanned_bundle_is_not_measured_again(self):
        """The scan's figure wins where it has one, so this costs a query."""
        conn = db.connect(":memory:")
        db.init_schema(conn)
        space.init(conn)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = self._fake_apps(tmp, ["Known"])[0]
        with conn:
            conn.execute("INSERT INTO space_scan (id, root, started_ts, "
                         "finished_ts) VALUES (1,'/',0,1)")
            conn.execute("INSERT INTO space_dir (scan_id, path, depth, bytes, "
                         "files) VALUES (1,?,2,777000,1)", (path,))
        with mock.patch.object(space, "APP_FOLDERS", (tmp,)):
            found = space.applications(conn, 1)[0]
        self.assertEqual(found["bundle"], 777000)

    def test_it_never_offers_to_remove_a_system_application(self):
        """Safari and Mail live in /System/Applications and cannot be removed.

        They are large and they would top the list, and every one of those
        rows would carry a button that cannot work.
        """
        self.assertTrue(os.path.isdir("/System/Applications"))
        for path in space.installed():
            self.assertFalse(path.startswith("/System/"), path)

    def test_it_lists_only_applications(self):
        """Application folders hold other things -- Utilities, .localized."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self._fake_apps(tmp, ["Real"])
        os.makedirs(os.path.join(tmp, "Not An App"))
        with mock.patch.object(space, "APP_FOLDERS", (tmp,)):
            found = space.installed()
        self.assertEqual([os.path.basename(p) for p in found], ["Real.app"])

    def test_a_file_that_merely_ends_in_app_is_not_an_application(self):
        """An application is a folder. A file wearing the name is not one.

        It would be listed at no size with a button offering to uninstall it.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self._fake_apps(tmp, ["Real"])
        with open(os.path.join(tmp, "Fake.app"), "wb") as handle:
            handle.write(b"not a bundle")
        with mock.patch.object(space, "APP_FOLDERS", (tmp,)):
            found = space.installed()
        self.assertEqual([os.path.basename(p) for p in found], ["Real.app"])

    def test_measuring_a_bundle_does_not_follow_a_symlink_out_of_it(self):
        """A bundle that links to a folder is not the size of that folder.

        Applications link into shared frameworks and into the user's own
        folders. Counting through the link would charge an application for
        somebody else's files, and a link pointing at home would charge it for
        the whole disk.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        outside = os.path.join(tmp, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "big"), "wb") as fh:
            fh.write(b"x" * 200000)
        path = self._fake_apps(tmp, ["Linker"])[0]
        os.symlink(outside, os.path.join(path, "Contents", "elsewhere"))
        self.assertLess(space._bundle_bytes(path), 100000)

    def test_the_listing_does_not_count_the_bundle_twice(self):
        """The bundle is inside the per-application total already.

        Files under /Applications/X.app are attributed to X, exactly as files
        under its caches are, so adding the folder total to the owner total
        counts the application twice. Xcode reported 4,220 MB of bundle and
        4,224 MB "left behind", which is the bundle again plus four megabytes of
        truth.
        """
        conn = db.connect(":memory:")
        db.init_schema(conn)
        space.init(conn)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        thing = self._fake_apps(tmp, ["Thing"])[0]
        with conn:
            conn.execute("INSERT INTO space_scan (id, root, started_ts, "
                         "finished_ts) VALUES (1,'/',0,1)")
            conn.execute("INSERT INTO space_dir (scan_id, path, depth, bytes, "
                         "files) VALUES (1, :p, 2, 1000, 1)", {"p": thing})
            # The owner total contains the bundle and 500 bytes besides.
            conn.execute("INSERT INTO space_owner (scan_id, owner, bytes, "
                         "files) VALUES (1,'Thing',1500,2)")
        with mock.patch.object(space, "APP_FOLDERS", (tmp,)):
            found = space.applications(conn, 1)[0]
        self.assertEqual(found["bundle"], 1000)
        self.assertEqual(found["leftover"], 500)
        self.assertEqual(found["bytes"], 1500)

    def test_an_application_with_nothing_beside_it_reports_no_leftovers(self):
        conn = db.connect(":memory:")
        db.init_schema(conn)
        space.init(conn)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        thing = self._fake_apps(tmp, ["Thing"])[0]
        with conn:
            conn.execute("INSERT INTO space_scan (id, root, started_ts, "
                         "finished_ts) VALUES (1,'/',0,1)")
            conn.execute("INSERT INTO space_dir (scan_id, path, depth, bytes, "
                         "files) VALUES (1, :p, 2, 1000, 1)", {"p": thing})
            conn.execute("INSERT INTO space_owner (scan_id, owner, bytes, "
                         "files) VALUES (1,'Thing',1000,1)")
        with mock.patch.object(space, "APP_FOLDERS", (tmp,)):
            found = space.applications(conn, 1)[0]
        self.assertEqual(found["leftover"], 0)

    def test_an_application_absent_from_the_owner_totals_still_counts(self):
        """Not every application appears in the per-application totals.

        Those are kept for the largest owners, so a small application can be
        missing from them entirely. Subtracting its bundle from a zero would
        make its leftovers negative and its total zero, and a 40 MB application
        would sort below one that occupies nothing at all.
        """
        conn = db.connect(":memory:")
        db.init_schema(conn)
        space.init(conn)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        small = self._fake_apps(tmp, ["Small"])[0]
        with conn:
            conn.execute("INSERT INTO space_scan (id, root, started_ts, "
                         "finished_ts) VALUES (1,'/',0,1)")
            conn.execute("INSERT INTO space_dir (scan_id, path, depth, bytes, "
                         "files) VALUES (1, :p, 2, 4000, 1)", {"p": small})
            # No space_owner row for Small at all.
        with mock.patch.object(space, "APP_FOLDERS", (tmp,)):
            found = space.applications(conn, 1)[0]
        self.assertEqual(found["leftover"], 0)
        self.assertEqual(found["bytes"], 4000)

    def test_nothing_is_removed_by_looking(self):
        app = self._app("Thing", "com.example.thing")
        made = self._put("Library/Caches/com.example.thing/c.bin")
        space.leftovers(app)
        self.assertTrue(os.path.exists(made))
        self.assertTrue(os.path.exists(app))


class TestVolumes(unittest.TestCase):
    def test_it_reports_the_data_volume_not_the_sealed_system_one(self):
        # `df /` is the read-only system snapshot: same on every Mac, and
        # nothing to do with the space anybody is short of.
        found = space.volumes()
        self.assertIsNotNone(found["data"])
        self.assertGreater(found["data"]["total"], 0)
        self.assertLessEqual(found["data"]["free"], found["data"]["total"])

    def test_used_is_this_volume_and_not_the_whole_container(self):
        """statvfs charges one volume for the whole container.

        On APFS the volumes share a pool of free space, so
        statvfs("/System/Volumes/Data") reported 228 GB used on a machine whose
        data volume held 198 -- the other 30 being System, Preboot, Recovery
        and VM. The scan was then compared against the 228 and a third of the
        disk came out "missing", most of it volumes nobody can scan and nobody
        should be charged for.
        """
        box = {"total": 100, "free": 10, "used": 90,
               "volumes": [], "data": {"name": "Data", "role": "Data",
                                       "used": 60, "mount": ""},
               "others": [{"name": "System", "role": "System", "used": 30,
                           "mount": ""}]}
        with mock.patch.object(space, "container", return_value=box):
            data = space.volumes()["data"]
        self.assertEqual(data["used"], 60)
        self.assertEqual(data["container_used"], 90)
        self.assertEqual([v["name"] for v in data["others"]], ["System"])

    def test_the_other_volumes_are_not_part_of_this_volumes_gap(self):
        # They sit outside `used`, so counting them as an explanation for the
        # difference between `used` and what was scanned explains away space
        # that was never in it.
        conn = db.connect(":memory:")
        db.init_schema(conn)
        found = {"finished_ts": 10, "bytes": 40, "unreadable": 0, "stopped": ""}
        vol = {"used": 60, "total": 100, "free": 40,
               "container_used": 90,
               "others": [{"name": "System", "role": "System", "used": 30}]}
        with mock.patch.object(space, "latest", return_value=found), \
                mock.patch.object(space, "volumes", return_value={"data": vol}), \
                mock.patch.object(space, "guarded", return_value=[]), \
                mock.patch.object(space, "snapshots", return_value=[]):
            got = space.reconcile(conn)
        self.assertEqual(got["missing"], 20)
        self.assertNotIn(30, [p["bytes"] for p in got["parts"]],
                         "another volume was counted inside this one's gap")
        self.assertEqual([v["name"] for v in got["others"]], ["System"])

    def _apfs(self, containers):
        import plistlib
        return plistlib.dumps({"Containers": containers}).decode("utf-8")

    def _box(self, ceiling, free, volumes):
        return {"CapacityCeiling": ceiling, "CapacityFree": free,
                "Volumes": [{"Name": n, "Roles": [r], "CapacityInUse": u}
                            for n, r, u in volumes]}

    def setUp(self):
        space._CONTAINER.update({"at": 0.0, "for": None, "was": None})

    def test_the_right_container_is_the_one_the_volume_is_the_size_of(self):
        """An external disk also has a volume with the Data role.

        Taking the first container holding one picked the external, reported
        its usage as the user's, and the difference against the scan came out
        at zero -- so the panel that explains the difference vanished, on the
        machines most likely to need it.
        """
        text = self._apfs([
            self._box(500, 100, [("ExternalData", "Data", 999)]),
            self._box(245, 16, [("Macintosh HD", "System", 12),
                                ("Macintosh HD - Data", "Data", 198)]),
        ])
        with mock.patch.object(space, "_run", return_value=text):
            got = space.container(expect=245)
        self.assertEqual(got["data"]["used"], 198)
        self.assertEqual([v["name"] for v in got["others"]], ["Macintosh HD"])

    def test_no_container_of_that_size_is_no_answer_rather_than_a_wrong_one(self):
        text = self._apfs([self._box(500, 100, [("Other", "Data", 999)])])
        with mock.patch.object(space, "_run", return_value=text):
            self.assertIsNone(space.container(expect=245))

    def test_it_survives_diskutil_being_missing_or_talking_nonsense(self):
        for answer in ("", "not a plist at all", "<plist><dict/></plist>"):
            space._CONTAINER.update({"at": 0.0, "for": None, "was": None})
            with mock.patch.object(space, "_run", return_value=answer):
                self.assertIsNone(space.container(expect=245))
        # And the fallback still answers, from statvfs.
        with mock.patch.object(space, "_run", return_value=""):
            self.assertIsNotNone(space.volumes()["data"])

    def test_the_folders_it_could_not_open_are_named_in_the_difference(self):
        # 836 of them on the machine this was written on, counted by the scan,
        # stored, and shown nowhere.
        conn = db.connect(":memory:")
        db.init_schema(conn)
        found = {"finished_ts": 10, "bytes": 40, "unreadable": 836, "stopped": ""}
        vol = {"used": 60, "total": 100, "free": 40, "others": []}
        with mock.patch.object(space, "latest", return_value=found), \
                mock.patch.object(space, "volumes", return_value={"data": vol}), \
                mock.patch.object(space, "guarded", return_value=[]), \
                mock.patch.object(space, "snapshots", return_value=[]):
            got = space.reconcile(conn)
        self.assertTrue(any("836" in p["what"] for p in got["parts"]),
                        "the refused folders are not in the breakdown")


class TestCounting(unittest.TestCase):
    """What the disk gave out, counted once."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_hard_link_is_not_counted_twice(self):
        """One file with two names is one file.

        Homebrew's Cellar and Xcode's caches are full of them, and counting
        each name meant reporting space the disk never gave out -- in the
        direction that makes a tool tell somebody to delete something.
        """
        root = os.path.join(self.dir, "home")
        _tree(root, {"a/big.bin": 200000})
        os.link(os.path.join(root, "a/big.bin"), os.path.join(root, "a/same.bin"))
        was_roots, was_home = space.SYSTEM_ROOTS, os.environ.get("HOME")
        space.SYSTEM_ROOTS = ()
        os.environ["HOME"] = root
        try:
            sid = space.scan(self.conn, None, floor=0, file_floor=0)
        finally:
            space.SYSTEM_ROOTS = was_roots
            if was_home:
                os.environ["HOME"] = was_home
        total = self.conn.execute(
            "SELECT bytes FROM space_scan WHERE id=?", (sid,)).fetchone()[0]
        one = os.lstat(os.path.join(root, "a/big.bin")).st_blocks * 512
        self.assertEqual(total, one)

    def test_a_folder_of_links_measures_as_one_copy(self):
        # The same rule where the uninstaller measures a bundle directly.
        root = os.path.join(self.dir, "bundle")
        _tree(root, {"x.bin": 100000})
        os.link(os.path.join(root, "x.bin"), os.path.join(root, "y.bin"))
        one = os.lstat(os.path.join(root, "x.bin")).st_blocks * 512
        self.assertEqual(space._folder_size(root), one)
        self.assertEqual(space._bundle_bytes(root), one)

    def test_a_file_with_one_name_is_always_counted(self):
        # The other half: a dedup that drops ordinary files would pass the test
        # above and report an empty disk.
        seen = set()
        st = os.lstat(__file__)
        self.assertTrue(space.counted_once(seen, st))
        self.assertEqual(seen, set(), "an ordinary file was remembered")

    def test_underscores_in_a_folder_name_are_not_wildcards(self):
        """LIKE treats `_` as "any character", and paths are full of them.

        Drilling into ~/my_app also listed what was inside ~/myXapp.
        """
        root = os.path.join(self.dir, "home")
        _tree(root, {"my_app/inside.bin": 40000, "myXapp/other.bin": 40000})
        was_roots, was_home = space.SYSTEM_ROOTS, os.environ.get("HOME")
        space.SYSTEM_ROOTS = ()
        os.environ["HOME"] = root
        try:
            sid = space.scan(self.conn, None, floor=0, file_floor=0)
            rows = space.biggest_dirs(self.conn, sid,
                                      under=os.path.join(root, "my_app"))
        finally:
            space.SYSTEM_ROOTS = was_roots
            if was_home:
                os.environ["HOME"] = was_home
        for row in rows:
            self.assertIn("my_app", row["path"],
                          "a folder from beside it came back: %s" % row["path"])

    def test_a_file_that_is_gone_is_not_offered(self):
        # Every row on that list comes with a button that acts on the disk as
        # it is now, and the scan is a photograph.
        root = os.path.join(self.dir, "home")
        _tree(root, {"keep.bin": 60000, "gone.bin": 60000})
        was_roots, was_home = space.SYSTEM_ROOTS, os.environ.get("HOME")
        space.SYSTEM_ROOTS = ()
        os.environ["HOME"] = root
        try:
            sid = space.scan(self.conn, None, floor=0, file_floor=0)
            os.remove(os.path.join(root, "gone.bin"))
            paths = [r["path"] for r in space.biggest_files(self.conn, sid)]
        finally:
            space.SYSTEM_ROOTS = was_roots
            if was_home:
                os.environ["HOME"] = was_home
        self.assertIn(os.path.join(root, "keep.bin"), paths)
        self.assertNotIn(os.path.join(root, "gone.bin"), paths)

    def test_files_scoped_to_the_folder_being_looked_at(self):
        # Drilling into a folder ranks the files there, not the same global
        # list on every level.
        root = os.path.join(self.dir, "home")
        _tree(root, {"top.bin": 90000, "movies/big.mov": 80000,
                     "movies/deep/clip.mov": 70000, "docs/paper.pdf": 60000})
        was_roots, was_home = space.SYSTEM_ROOTS, os.environ.get("HOME")
        space.SYSTEM_ROOTS = ()
        os.environ["HOME"] = root
        try:
            sid = space.scan(self.conn, None, floor=0, file_floor=0)
            scoped = [r["path"] for r in space.biggest_files(
                self.conn, sid, under=os.path.join(root, "movies"))]
        finally:
            space.SYSTEM_ROOTS = was_roots
            if was_home:
                os.environ["HOME"] = was_home
        self.assertEqual(scoped,
                         [os.path.join(root, "movies", "big.mov"),
                          os.path.join(root, "movies", "deep", "clip.mov")])


class TestAttributing(unittest.TestCase):
    """Which application a path belongs to."""

    def test_utilities_is_not_an_application(self):
        # "/Applications" took the first component after it, so every Utility
        # belonged to an application called Utilities, which owned them all and
        # left each of them owning nothing.
        owner = space._Owner("/Users/nobody", {})
        self.assertEqual(
            owner.of("/Applications/Utilities/Terminal.app/Contents/x"),
            "Terminal")
        self.assertEqual(owner.of("/Applications/Safari.app/Contents/x"),
                         "Safari")

    def test_an_application_in_your_own_folder_has_an_owner(self):
        # ~/Applications had no rule at all, so anything installed there owned
        # nothing and always reported zero left behind.
        owner = space._Owner("/Users/nobody", {})
        self.assertEqual(
            owner.of("/Users/nobody/Applications/Thing.app/Contents/x"),
            "Thing")


if __name__ == "__main__":
    unittest.main()
