"""The one-press cleanup plan: safe caches plus removed applications' leavings."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from procwatch import space


def _fill(root, spec):
    for rel, size in spec.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"\0" * size)


class TestReverseDns(unittest.TestCase):
    def test_identifier_shapes(self):
        self.assertEqual(space._reverse_dns("com.foo.Bar"), "com.foo.Bar")
        self.assertEqual(space._reverse_dns("com.foo.Bar.savedState"),
                         "com.foo.Bar")

    def test_non_identifiers_are_refused(self):
        # A vendor folder, a bare tool name, and a two-part id are all too
        # little to match an application against.
        for name in ("Google", "com.foo", "..", "com..Bar"):
            self.assertEqual(space._reverse_dns(name), "", name)


class TestOrphans(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _orphans(self, installed):
        with mock.patch.object(space, "_ORPHAN_DIRS", (self.dir,)):
            return space.orphans(installed_idents=installed)

    def test_only_the_gone_and_only_identifiers(self):
        _fill(self.dir, {
            "com.gone.App/blob": 4096,          # gone -> listed
            "com.present.App/blob": 4096,       # still installed -> kept
            "com.apple.dock/blob": 4096,        # macOS itself -> kept
            "Google/blob": 4096,                # not an identifier -> kept
        })
        found = self._orphans({"com.present.App"})
        self.assertEqual([item["ident"] for item in found], ["com.gone.App"])
        self.assertGreater(found[0]["bytes"], 0)

    def test_an_installed_vendors_helpers_are_kept(self):
        # com.microsoft.teams2's helper caches share the vendor prefix with
        # the installed com.microsoft.Word; absence from /Applications does
        # not make them orphans.
        _fill(self.dir, {"com.microsoft.teams2/blob": 4096})
        self.assertEqual(self._orphans({"com.microsoft.Word"}), [])

    def test_empty_folders_are_not_news(self):
        os.makedirs(os.path.join(self.dir, "com.gone.App"))
        self.assertEqual(self._orphans(set()), [])


class TestCleanupPlan(unittest.TestCase):
    def test_groups_and_total(self):
        caches = [{"path": "~/x", "full_path": "/home/x", "bytes": 10,
                   "files": 1, "why": "safe"}]
        gone = [{"path": "/home/y", "display": "~/y", "bytes": 5,
                 "ident": "com.a.b"}]
        with mock.patch.object(space, "caches", return_value=caches), \
                mock.patch.object(space, "orphans", return_value=gone):
            plan = space.cleanup_plan()
        self.assertEqual([g["key"] for g in plan["groups"]],
                         ["caches", "orphans"])
        self.assertEqual(plan["bytes"], 15)

    def test_an_orphan_already_on_the_cache_list_is_counted_once(self):
        caches = [{"path": "~/x", "full_path": "/home/x", "bytes": 10,
                   "files": 1, "why": "safe"}]
        gone = [{"path": "/home/x", "display": "~/x", "bytes": 10,
                 "ident": "com.a.b"},
                {"path": "/home/x/inside", "display": "~/x/inside",
                 "bytes": 4, "ident": "com.c.d"}]
        with mock.patch.object(space, "caches", return_value=caches), \
                mock.patch.object(space, "orphans", return_value=gone):
            plan = space.cleanup_plan()
        self.assertEqual([g["key"] for g in plan["groups"]], ["caches"])
        self.assertEqual(plan["bytes"], 10)

    def test_nothing_to_do_is_an_empty_plan(self):
        with mock.patch.object(space, "caches", return_value=[]), \
                mock.patch.object(space, "orphans", return_value=[]):
            plan = space.cleanup_plan()
        self.assertEqual(plan, {"groups": [], "bytes": 0})


if __name__ == "__main__":
    unittest.main()
