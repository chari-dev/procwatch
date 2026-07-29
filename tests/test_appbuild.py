import os
import shutil
import tempfile
import unittest

from procwatch import appbuild


class TestAssets(unittest.TestCase):
    """The build inputs must be reachable in both shapes the tool ships in.

    From a checkout they are files in menubar/. From the single-file build
    there is no such directory and they come from the embedded table. A
    missing input has to fail loudly at build time rather than produce a
    bundle with no icon or, worse, no executable.
    """

    def test_every_input_resolves_in_a_checkout(self):
        for name in appbuild.ASSETS:
            self.assertTrue(appbuild.asset(name), name)

    def test_the_swift_source_is_the_real_one(self):
        source = appbuild.asset("ProcwatchBar.swift").decode()
        self.assertIn("NSStatusBar", source)
        self.assertIn("WKWebView", source)

    def test_an_unknown_input_raises(self):
        with self.assertRaises(RuntimeError):
            appbuild.asset("not-a-thing.png")

    def test_embedded_assets_are_used_when_there_is_no_checkout(self):
        # Simulates the single-file build: no menubar/ directory to read from.
        real = appbuild._repo_menubar
        appbuild._repo_menubar = lambda: None
        appbuild.EMBEDDED["procwatch-bar.png"] = "aGVsbG8="      # "hello"
        try:
            self.assertEqual(appbuild.asset("procwatch-bar.png"), b"hello")
        finally:
            appbuild._repo_menubar = real
            appbuild.EMBEDDED.pop("procwatch-bar.png", None)


class TestBundleLayout(unittest.TestCase):
    def test_the_plist_names_the_executable_that_is_built(self):
        # A mismatch here produces a bundle that installs and cannot launch,
        # with no error anywhere.
        self.assertIn("<string>ProcwatchBar</string>", appbuild.INFO_PLIST)

    def test_the_plist_hides_the_dock_icon(self):
        # A menu bar app with a Dock icon and no window is a bug people see.
        self.assertIn("<key>LSUIElement</key><true/>", appbuild.INFO_PLIST)

    def test_the_display_name_matches_the_quit_target(self):
        # `quit app "Procwatch"` is how a running copy is asked to exit before
        # its bundle is replaced; if the name drifts it silently stops working.
        self.assertIn("<key>CFBundleName</key><string>Procwatch</string>",
                      appbuild.INFO_PLIST)
        self.assertEqual(appbuild.APP_NAME, "Procwatch.app")

    def test_the_superseded_bundle_name_is_still_cleaned_up(self):
        # 1.0.0 and 1.1.x installed it as ProcwatchBar.app. Left behind it is a
        # second Launchpad icon running older code.
        self.assertIn("ProcwatchBar.app", appbuild.SUPERSEDED)


class TestPreconditions(unittest.TestCase):
    def test_it_refuses_to_build_without_swiftc(self):
        real = appbuild.have_swift
        appbuild.have_swift = lambda: False
        try:
            with self.assertRaises(RuntimeError) as caught:
                appbuild.build(tempfile.mkdtemp())
            self.assertIn("xcode-select", str(caught.exception))
        finally:
            appbuild.have_swift = real


if __name__ == "__main__":
    unittest.main()
