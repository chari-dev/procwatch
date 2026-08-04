"""The storage page, and the move that created it.

Working out where a disk went used to be a sheet thrown over the dashboard.
It is now its own page at /disk, the way the network monitor is its own page
at /net. These guard the three ways that move can go wrong: the page not
loading, the route or the bundle forgetting it exists, and the dashboard still
holding a door to the sheet that is no longer there.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATIC = os.path.join(ROOT, "procwatch", "static")
PAGE = os.path.join(STATIC, "storage.html")
sys.path.insert(0, os.path.join(ROOT, "tools"))


def _script(path):
    with open(path) as handle:
        return re.findall(r"<script>(.*?)</script>", handle.read(), re.S)[-1]


class TestStoragePageLoads(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_script_runs_to_completion(self):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(_script(PAGE))
            path = handle.name
        try:
            result = subprocess.run(
                ["node", os.path.join(HERE, "harness.mjs"), path],
                capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0,
                             "the storage page threw while loading:\n"
                             + result.stdout + result.stderr)
        finally:
            os.unlink(path)

    def test_it_carries_the_token_placeholder(self):
        """Starting a scan is a POST and goes through the same guard the
        dashboard uses. Without the placeholder the page renders and every
        scan is refused."""
        with open(PAGE) as handle:
            self.assertIn("__PROCWATCH_TOKEN__", handle.read())


class TestItIsWiredUp(unittest.TestCase):
    def test_the_server_routes_disk(self):
        with open(os.path.join(ROOT, "procwatch", "server.py")) as handle:
            source = handle.read()
        self.assertIn('parsed.path == "/disk"', source)
        self.assertIn("def storage_html", source)

    def test_the_single_file_build_carries_the_page(self):
        """The checkout reads static/ from disk; the shipped copy cannot.
        A page missing from the bundle is a 404 only after release."""
        with open(os.path.join(ROOT, "tools", "bundle.py")) as handle:
            source = handle.read()
        names = re.findall(r'\("([^"]+\.(?:html|js))",\s*"(_[A-Z0-9_]+)"\)',
                           source)
        self.assertIn("storage.html", [name for name, _ in names],
                      "storage.html is not in the bundle's asset list, so the "
                      "shipped single file cannot serve /disk")
        # Embedding it is only half: the served copy has to be pointed at it.
        self.assertIn("_server.storage_html = _storage_embedded", source)
        self.assertIn("_STORAGE_HTML_B64", source)

    def test_the_menu_bar_offers_it(self):
        with open(os.path.join(ROOT, "menubar", "ProcwatchBar.swift")) as handle:
            swift = handle.read()
        self.assertIn('"Storage…"', swift)
        self.assertIn("openStorage", swift)

    def test_it_opens_at_the_size_the_monitor_opens_at(self):
        """Three panes do not fit in a quarter of the screen."""
        with open(os.path.join(ROOT, "menubar", "ProcwatchBar.swift")) as handle:
            swift = handle.read()
        self.assertIn("func isInstrument", swift)
        self.assertIn('path.hasPrefix("/disk")', swift)


class TestItPostsWhereTheServerListens(unittest.TestCase):
    """The first version of this page posted a scan to /api/space.

    That path is not on do_POST's allow-list, so the server answered 404, the
    page's catch swallowed it, and the button appeared to do nothing at all --
    no error, no scan, no sign anything had happened. A mutating call to a
    path the server does not accept should fail here, not in someone's hands.
    """

    def allowed(self):
        with open(os.path.join(ROOT, "procwatch", "server.py")) as handle:
            source = handle.read()
        start = source.index("def do_POST")
        tuple_start = source.index("parsed.path not in (", start)
        block = source[tuple_start:source.index("):", tuple_start)]
        return set(re.findall(r'"(/api/[a-z]+)"', block))

    def used(self):
        with open(PAGE) as handle:
            page = handle.read()
        return set(re.findall(r'post\("(/api/[a-z]+)"', page))

    def test_every_post_the_page_makes_is_accepted(self):
        stray = self.used() - self.allowed()
        self.assertEqual(stray, set(),
                         "the storage page POSTs to paths the server rejects: "
                         + ", ".join(sorted(stray)))

    def test_it_actually_posts_somewhere(self):
        """Guards the guard: a page that posts nowhere would pass the above."""
        self.assertIn("/api/scan", self.used())
        self.assertIn("/api/trash", self.used())

    def test_it_sends_every_header_the_guard_requires(self):
        """_reject_cross_origin wants all three. Sending two is a 403 that
        looks exactly like a button that does nothing -- which is how both the
        scan and the Trash buttons first shipped."""
        with open(PAGE) as handle:
            page = handle.read()
        for header in ('"Content-Type": "application/json"',
                       '"X-Procwatch": "1"',
                       '"X-Procwatch-Token"'):
            self.assertIn(header, page,
                          "the storage page's POSTs omit %s, which the "
                          "server's cross-origin guard requires" % header)

    def test_a_failed_post_is_reported_rather_than_swallowed(self):
        with open(PAGE) as handle:
            page = handle.read()
        self.assertIn("if (!r.ok)", page)
        self.assertIn("Could not start the scan", page)


class TestTheDashboardLetGo(unittest.TestCase):
    def markup(self):
        with open(os.path.join(STATIC, "index.html")) as handle:
            return handle.read()

    def test_the_sheet_is_gone(self):
        self.assertNotIn('id="spacesheet"', self.markup())

    def test_nothing_still_opens_the_sheet(self):
        """A leftover click handler would open the removed sheet and follow
        the link, which is how you ship both halves of a move."""
        markup = self.markup()
        self.assertNotIn('on("spaceopen"', markup)
        self.assertNotIn('on("spaceclose"', markup)

    def test_the_card_is_gone_entirely(self):
        """Not left as a summary with a link. A card that repeats a page's
        headline is a second place for the same answer to go stale."""
        markup = self.markup()
        self.assertNotIn('id="storagecard"', markup)
        self.assertNotIn('id="storagebody"', markup)

    def test_the_sleep_card_is_gone_too(self):
        """Same move, same reason: it lives at /battery now."""
        markup = self.markup()
        self.assertNotIn('id="sleepcard"', markup)
        self.assertNotIn('id="sleepbody"', markup)


if __name__ == "__main__":
    unittest.main()
