import os
import plistlib
import sys
import types
import unittest
from procwatch import config, launchd


class TestPlist(unittest.TestCase):
    def setUp(self):
        self.plist = plistlib.loads(
            launchd.plist_text(["/usr/bin/python3", "-m", "procwatch.main"],
                               "/opt/procwatch").encode())

    def test_the_interval_matches_the_configured_sample_rate(self):
        self.assertEqual(self.plist["StartInterval"], config.INTERVAL)

    def test_it_runs_an_argument_vector_not_a_shell_string(self):
        self.assertEqual(
            self.plist["ProgramArguments"],
            ["/usr/bin/python3", "-m", "procwatch.main"])

    def test_it_does_not_keep_the_process_alive_between_ticks(self):
        # A resident daemon is precisely what this tool exists to detect.
        self.assertFalse(self.plist.get("KeepAlive", False))

    def test_the_label_matches_the_plist_filename(self):
        self.assertTrue(launchd.PLIST_PATH.endswith(self.plist["Label"] + ".plist"))


class TestEntryPoint(unittest.TestCase):
    """The agent has to be re-invokable from both shapes the tool ships in.

    Getting this wrong is silent -- launchd accepts the job and every run
    fails -- so each shape is asserted rather than assumed.
    """

    def setUp(self):
        self.real_package = sys.modules.get("procwatch")
        self.real_argv = list(sys.argv)

    def tearDown(self):
        sys.modules["procwatch"] = self.real_package
        sys.argv[:] = self.real_argv

    def _as_bundle(self, argv0):
        # The single-file build registers a package whose __path__ is empty:
        # there is no directory for `-m` to resolve against.
        fake = types.ModuleType("procwatch")
        fake.__path__ = []
        sys.modules["procwatch"] = fake
        sys.argv[:] = [argv0]

    def test_a_checkout_runs_the_module_from_the_repository_root(self):
        arguments, working_dir = launchd.entry_point()
        self.assertEqual(arguments[1:], ["-m", "procwatch.main"])
        self.assertTrue(os.path.isdir(os.path.join(working_dir, "procwatch")))

    def test_the_single_file_build_runs_the_script_by_absolute_path(self):
        script = os.path.abspath(__file__.replace("test_launchd.py", "fake.py"))
        with open(script, "w") as handle:
            handle.write("# stand-in for the generated bundle\n")
        try:
            self._as_bundle(script)
            arguments, working_dir = launchd.entry_point()
            self.assertEqual(arguments[1:], [script, "record"])
            self.assertEqual(working_dir, os.path.dirname(script))
            # -m would resolve to nothing here, which is the whole point.
            self.assertNotIn("-m", arguments)
        finally:
            os.remove(script)

    def test_it_refuses_to_schedule_a_job_it_cannot_re_invoke(self):
        # Better to fail at install time than to install a job that fails
        # every thirty seconds for the rest of the machine's life.
        self._as_bundle("")
        with self.assertRaises(RuntimeError):
            launchd.entry_point()

    def test_a_relative_invocation_is_recorded_as_an_absolute_path(self):
        # launchd runs with its own working directory; a relative path would
        # resolve somewhere else entirely.
        here = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(here, "fake_rel.py")
        with open(script, "w") as handle:
            handle.write("# stand-in\n")
        cwd = os.getcwd()
        try:
            os.chdir(here)
            self._as_bundle("fake_rel.py")
            arguments, _ = launchd.entry_point()
            self.assertEqual(arguments[1], script)
        finally:
            os.chdir(cwd)
            os.remove(script)


if __name__ == "__main__":
    unittest.main()
