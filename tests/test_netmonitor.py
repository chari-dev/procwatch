"""The network monitor has to survive being loaded, same as the dashboard.

test_dashboard.py has guarded index.html against the whole-script-aborts
failure since three releases shipped it. netmonitor.html -- the globe, the
scrubber and the peer history -- had no such guard, so the same class of fault
could ship there unnoticed, with the same symptom: a page that renders its
markup, runs none of its script, and looks merely empty.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(os.path.dirname(HERE), "procwatch", "static")
PAGE = os.path.join(STATIC, "netmonitor.html")


def _markup():
    with open(PAGE) as handle:
        return handle.read()


def _script():
    """The page's own script, with /world.js in front of it.

    The browser loads world.js first and the page's script uses what it
    defines, so running the page's script alone would fail on a missing
    global that is not actually missing.
    """
    with open(os.path.join(STATIC, "world.js")) as handle:
        world = handle.read()
    return world + "\n" + re.findall(r"<script>(.*?)</script>", _markup(), re.S)[-1]


class TestNetMonitorLoads(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_script_runs_to_completion(self):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(_script())
            path = handle.name
        try:
            result = subprocess.run(
                ["node", os.path.join(HERE, "harness.mjs"), path],
                capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0,
                             "the network monitor threw while loading:\n"
                             + result.stdout + result.stderr)
        finally:
            os.unlink(path)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_script_parses(self):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(_script())
            path = handle.name
        try:
            result = subprocess.run(["node", "--check", path],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            os.unlink(path)


class TestScrubberMarkup(unittest.TestCase):
    def test_the_scrubber_is_gone(self):
        """Removed as unhelpful. The span buttons already pick the window, and
        a second control for the same thing was one more thing to explain."""
        markup = _markup()
        for name in ("scrub", "scrubtime", "scrubtrack", "scrubcanvas",
                     "scrubhead", "scrubnow"):
            self.assertNotIn('id="%s"' % name, markup)

    def test_nothing_still_calls_it(self):
        """Leaving a call to a removed drawScrub throws on the first tick and
        takes the whole page with it."""
        script = _script()
        for name in ("drawScrub", "scrubBounds", "scrubTime"):
            self.assertNotIn(name, script)

    def test_the_map_draws_history_from_the_peer_endpoint(self):
        """The bug this whole feature exists to fix: a past window drew from
        the live snapshot, which describes only this instant, so every span
        other than Live produced an empty globe."""
        script = _script()
        self.assertIn("/api/netmap", script)
        self.assertIn("PASTMAP", script)

    def test_the_window_keeps_its_width_while_scrubbing(self):
        """Dragging moves the end of the window; it does not stretch it."""
        script = _script()
        self.assertIn("end - SPAN", script)

    def test_a_past_window_is_not_short_circuited_before_it_draws(self):
        """The bug that made the whole peer history invisible.

        drawGlobe used to return early on any past span, with a note saying
        peers were not recorded. That was true before the history existed and
        false afterwards, so the endpoint served 31 placed servers to a globe
        that had already decided not to draw anything.
        """
        script = _script()
        self.assertNotIn("is nothing to place on the globe", script)
        self.assertNotIn("not which servers it spoke to", script)

    def test_the_inspector_no_longer_claims_servers_are_unrecorded(self):
        script = _script()
        self.assertNotIn("not to whom", script)
        self.assertIn("pastPeerRows", script)


if __name__ == "__main__":
    unittest.main()
