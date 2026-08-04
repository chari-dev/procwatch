"""Peers are shown by who owns them, not by their address.

The network monitor listed "146.75.92.158" where it meant Fastly. Most
addresses have no reverse-DNS name, so the page waited for one that was never
coming and fell back to the number. These pin the replacement: the owner from
the address lookup, tidied.
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

# The real values this machine's address lookup returned, left to right:
# what the service says, and what the column should read.
CASES = [
    ("FASTLY", "Fastly"),
    ("Fastly, Inc.", "Fastly"),
    ("Google LLC", "Google"),
    ("Apple Inc.", "Apple"),
    ("Cloudflare, Inc.", "Cloudflare"),
    ("GitHub, Inc.", "GitHub"),
    ("Amazon Technologies Inc.", "Amazon Technologies"),
    ("Akamai Technologies, Inc.", "Akamai Technologies"),
    ("Vercel, Inc", "Vercel"),
    ("", ""),
    # Short all-capitals names are acronyms spelled that way on purpose, not
    # a registry shouting. Folding them gives "Ibm", which is simply wrong.
    ("IBM", "IBM"),
    ("OVH SAS", "OVH SAS"),
    ("KDDI CORPORATION", "KDDI"),
    ("NTT Communications Corporation", "NTT Communications"),
]


def _script():
    with open(PAGE) as handle:
        return re.findall(r"<script>(.*?)</script>", handle.read(), re.S)[-1]


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PeerNameTest(unittest.TestCase):
    def run_js(self, tail):
        """Run the page under the shared harness, then evaluate `tail`.

        The probe is appended to the page's own script rather than run beside
        it, because the harness executes the script inside one Function scope
        -- so a `var` defined there is reachable from code appended to it and
        from nowhere else. Reusing the harness also means these tests get
        every browser stand-in it already has, instead of a second, thinner
        copy that goes stale.
        """
        with open(os.path.join(STATIC, "world.js")) as handle:
            world = handle.read()
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(world + "\n" + _script() + "\n" + tail)
            path = handle.name
        try:
            return subprocess.run(
                ["node", os.path.join(HERE, "harness.mjs"), path],
                capture_output=True, text=True, timeout=90)
        finally:
            os.unlink(path)

    def output(self, done):
        """What the probe printed, with the harness's own verdict removed."""
        self.assertEqual(done.returncode, 0,
                         "the page threw:\n" + done.stdout + done.stderr)
        return [line for line in done.stdout.strip().splitlines()
                if line != "OK"]

    def test_the_owner_is_tidied_for_display(self):
        # Marked so a blank result is still a line, rather than vanishing out
        # of the output and silently shifting every comparison after it.
        probe = ";".join('console.log("<" + tidyOrg("%s") + ">")' % raw
                         for raw, _ in CASES)
        got = self.output(self.run_js(probe))
        self.assertEqual(got, ["<%s>" % want for _, want in CASES])

    def test_two_spellings_of_one_company_become_one_row(self):
        """FASTLY and "Fastly, Inc." are the same place and were two rows."""
        got = self.output(self.run_js(
            'console.log(tidyOrg("FASTLY") === tidyOrg("Fastly, Inc."))'))
        self.assertEqual(got, ["true"])

    def test_an_address_falls_back_to_its_owner(self):
        got = self.output(self.run_js(
            'console.log(domainOf("146.75.92.158","146.75.92.158","FASTLY"))'))
        self.assertEqual(got, ["Fastly"])

    def test_a_real_name_still_wins_over_the_owner(self):
        """A published name is more specific than who owns the range."""
        got = self.output(self.run_js(
            'console.log(domainOf("lb-140-82-121-4.github.com",'
            '"140.82.121.4","Fastly, Inc."))'))
        self.assertEqual(got, ["github.com"])

    def test_an_address_with_no_owner_is_still_shown(self):
        """Better the number than an empty row."""
        got = self.output(self.run_js(
            'console.log(domainOf("10.1.2.3","10.1.2.3",""))'))
        self.assertEqual(got, ["10.1.2.3"])

    def test_mixed_case_names_keep_their_own_capitals(self):
        got = self.output(self.run_js(
            'console.log(tidyOrg("iCloud Private Relay"))'))
        self.assertEqual(got, ["iCloud Private Relay"])


class TrackRangeTest(unittest.TestCase):
    def test_the_scrubber_track_covers_the_whole_history(self):
        """The track is drawn across the full scrubbable range, so asking the
        server for only the current window leaves it blank everywhere you have
        not already been -- which reads as no traffic rather than no data."""
        with open(os.path.join(os.path.dirname(HERE), "procwatch",
                               "server.py")) as handle:
            source = handle.read()
        block = source[source.index('if path == "/api/netmap"'):]
        block = block[:block.index('if path == "/api/nettraffic"')]
        self.assertIn("track_start", block)
        self.assertIn("netpeer.timeline(conn, track_start, track_end)", block)
        self.assertNotIn("netpeer.timeline(conn, start, end)", block)


if __name__ == "__main__":
    unittest.main()
