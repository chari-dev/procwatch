"""Application icons: finding them, converting them once, and refusing to
read anything that is not an application."""
import os
import plistlib
import shutil
import tempfile
import unittest
from unittest import mock

from procwatch import icons


def _bundle(root, name, icon_file="AppIcon", make_icns=True, extra=()):
    """Build a plausible .app on disk."""
    app = os.path.join(root, name + ".app")
    resources = os.path.join(app, "Contents", "Resources")
    os.makedirs(resources)
    info = {"CFBundleName": name}
    if icon_file:
        info["CFBundleIconFile"] = icon_file
    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as handle:
        plistlib.dump(info, handle)
    if make_icns:
        target = (icon_file if icon_file.endswith(".icns")
                  else icon_file + ".icns")
        open(os.path.join(resources, target), "wb").close()
    for other in extra:
        open(os.path.join(resources, other), "wb").close()
    return app


class TestBundleOf(unittest.TestCase):
    def test_an_executable_inside_a_bundle(self):
        self.assertEqual(
            icons.bundle_of("/Applications/Arc.app/Contents/MacOS/Arc"),
            "/Applications/Arc.app")

    def test_a_bundle_path_is_already_one(self):
        self.assertEqual(icons.bundle_of("/Applications/Arc.app"),
                         "/Applications/Arc.app")

    def test_a_helper_belongs_to_the_application_that_carries_it(self):
        # Applications keep helpers in their own .app bundles inside
        # Contents/Frameworks, and those have no icon. The one worth showing
        # is the application's.
        self.assertEqual(
            icons.bundle_of("/Applications/GitHub Desktop.app/Contents/"
                            "Frameworks/GitHub Desktop Helper.app/Contents/"
                            "MacOS/GitHub Desktop Helper"),
            "/Applications/GitHub Desktop.app")

    def test_a_plain_program_belongs_to_no_bundle(self):
        # The daemons: they keep their initials, and that is the correct
        # answer rather than a missing image.
        for path in ("/usr/sbin/mDNSResponder", "/bin/ls", "", None):
            self.assertEqual(icons.bundle_of(path), "")


class TestFindingTheIcon(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_the_named_icon_wins(self):
        app = _bundle(self.dir, "Named", icon_file="AppIcon",
                      extra=("Toolbar.icns",))
        self.assertTrue(icons._icns_path(app).endswith("AppIcon.icns"))

    def test_a_name_without_the_extension_still_resolves(self):
        app = _bundle(self.dir, "Bare", icon_file="AppIcon")
        self.assertTrue(icons._icns_path(app).endswith("AppIcon.icns"))

    def test_one_unnamed_icon_is_unambiguous(self):
        app = _bundle(self.dir, "Lonely", icon_file="", make_icns=False,
                      extra=("Whatever.icns",))
        self.assertTrue(icons._icns_path(app).endswith("Whatever.icns"))

    def test_a_drawer_full_of_icons_with_no_name_is_refused(self):
        # Several .icns and nothing saying which is the application's: any
        # pick would be a guess, and a toolbar glyph beside the name reads
        # as the application's own icon.
        app = _bundle(self.dir, "Many", icon_file="", make_icns=False,
                      extra=("Toolbar.icns", "Document.icns", "Badge.icns"))
        self.assertEqual(icons._icns_path(app), "")

    def test_a_conventional_name_is_taken_from_a_crowd(self):
        app = _bundle(self.dir, "Crowd", icon_file="", make_icns=False,
                      extra=("Toolbar.icns", "AppIcon.icns", "Doc.icns"))
        self.assertTrue(icons._icns_path(app).endswith("AppIcon.icns"))

    def test_a_bundle_with_no_resources_says_nothing(self):
        app = os.path.join(self.dir, "Empty.app")
        os.makedirs(os.path.join(app, "Contents"))
        self.assertEqual(icons._icns_path(app), "")


class TestWhatMayBeAskedFor(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_a_real_application_is_allowed(self):
        real = "/System/Applications/Mail.app"
        if not os.path.isdir(real):
            self.skipTest("no Mail.app on this machine")
        self.assertTrue(icons.allowed(real))

    def test_somewhere_else_on_the_disk_is_not(self):
        app = _bundle(self.dir, "Sneaky")
        self.assertFalse(icons.allowed(app))

    def test_things_that_are_not_bundles_are_not(self):
        for path in ("", "/etc/passwd", "/Applications", None,
                     "/Applications/../etc/passwd"):
            self.assertFalse(icons.allowed(path), path)

    def test_climbing_out_of_an_allowed_folder_is_refused(self):
        # normpath collapses the "..", so this is judged as what it actually
        # points at rather than as the folder it starts in.
        self.assertFalse(icons.allowed("/Applications/../tmp/Sneaky.app"))

    def test_a_firmlinked_system_application_is_allowed(self):
        # macOS puts Safari on a Cryptex volume and firmlinks it into
        # /Applications. Resolving the link refused it its own icon.
        safari = "/Applications/Safari.app"
        if not os.path.isdir(safari):
            self.skipTest("no Safari on this machine")
        self.assertTrue(icons.allowed(safari))

    def test_the_endpoint_returns_nothing_for_a_refused_path(self):
        app = _bundle(self.dir, "Sneaky")
        self.assertIsNone(icons.png(app))


class TestConversion(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cache = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cache, True)
        patch = mock.patch.object(icons, "cache_dir", return_value=self.cache)
        patch.start()
        self.addCleanup(patch.stop)
        self.app = _bundle(self.dir, "Test")
        allow = mock.patch.object(icons, "allowed", return_value=True)
        allow.start()
        self.addCleanup(allow.stop)

    def test_it_converts_once_and_serves_the_cache_after(self):
        def convert(argv, **kwargs):
            with open(argv[argv.index("--out") + 1], "wb") as handle:
                handle.write(b"\x89PNG-pretend")
            return mock.Mock(returncode=0)

        with mock.patch.object(icons.subprocess, "run",
                               side_effect=convert) as run:
            first = icons.png(self.app)
            second = icons.png(self.app)
        self.assertEqual(first, b"\x89PNG-pretend")
        self.assertEqual(second, first)
        self.assertEqual(run.call_count, 1)

    def test_an_application_with_no_icon_is_remembered_as_having_none(self):
        bare = _bundle(self.dir, "Bare", icon_file="", make_icns=False)
        with mock.patch.object(icons.subprocess, "run") as run:
            self.assertIsNone(icons.png(bare))
            self.assertIsNone(icons.png(bare))
        # sips is never asked about a bundle with no .icns at all.
        self.assertFalse(run.called)

    def test_a_conversion_that_fails_is_not_retried_forever(self):
        with mock.patch.object(icons.subprocess, "run",
                               return_value=mock.Mock(returncode=1)) as run:
            self.assertIsNone(icons.png(self.app))
            self.assertIsNone(icons.png(self.app))
        self.assertEqual(run.call_count, 1)

    def test_an_odd_size_falls_back_to_the_default(self):
        with mock.patch.object(icons.subprocess, "run",
                               return_value=mock.Mock(returncode=1)) as run:
            icons.png(self.app, 999)
        self.assertIn(str(icons.DEFAULT_SIZE), run.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
