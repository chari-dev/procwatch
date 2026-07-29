import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(os.path.dirname(HERE), "procwatch", "static", "index.html")


def _script():
    with open(PAGE) as handle:
        return re.findall(r"<script>(.*?)</script>", handle.read(), re.S)[-1]


class TestDashboardLoads(unittest.TestCase):
    """The page has to survive being loaded.

    Every JavaScript fault so far has had the same symptom -- the whole script
    aborts on the line that fails, nothing after it ever runs, and the reader
    sees "Loading" forever with no error anywhere. A missing element, a
    variable used above its definition, a reference left behind by a rewrite:
    all three shipped, and all three would have failed here.
    """

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
                             "the dashboard threw while loading:\n" + result.stdout)
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


if __name__ == "__main__":
    unittest.main()
