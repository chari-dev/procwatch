"""What the sharing port serves, now that it serves the network monitor too.

The line this file guards is the same one share.py exists to draw: this port
can read a machine's history and nothing else. Adding pages to it must not
add a way to act on the machine.
"""
import inspect
import unittest

from procwatch import server, share


class TestPages(unittest.TestCase):
    def test_the_monitor_is_a_page_so_a_browser_is_asked_for_the_key(self):
        # Without this, opening /net without a key answered a browser with a
        # line of JSON instead of the box that asks for one.
        handler = share.ShareHandler.__new__(share.ShareHandler)
        for path in ("/", "/index.html", "/net"):
            self.assertTrue(handler._wants_page(path), path)
        for path in ("/api/info", "/world.js", "/api/icon"):
            self.assertFalse(handler._wants_page(path), path)

    def test_it_can_send_the_monitor_and_the_dashboard(self):
        for name in ("_send_monitor", "_send_dashboard", "_send_asset"):
            self.assertTrue(hasattr(share.ShareHandler, name), name)

    def test_the_monitor_it_sends_is_the_one_the_local_server_sends(self):
        # One monitor, not a cut-down copy that drifts.
        source = inspect.getsource(share.ShareHandler._send_monitor)
        self.assertIn("server.netmonitor_html()", source)

    def test_the_monitor_is_marked_read_only(self):
        source = inspect.getsource(share.ShareHandler._send_monitor)
        self.assertIn("procwatch-readonly", source)
        # And the token is emptied rather than left as the placeholder.
        self.assertIn('"__PROCWATCH_TOKEN__", ""', source)

    def test_the_page_hides_the_off_switch_when_read_only(self):
        page = server.netmonitor_html()
        self.assertIn("procwatch-readonly", page)
        self.assertIn("READONLY", page)
        # The switch is only rendered on the branch that is not read-only.
        self.assertIn("READONLY", page[:page.index('data-block=')])


class TestTheMonitorHasFiguresToShow(unittest.TestCase):
    def test_sharing_starts_the_network_pass(self):
        """The monitor's figures come from a background nettop pass, and only
        the local server had ever started one -- so a shared machine answered
        with an empty list forever and the globe had nothing on it."""
        source = inspect.getsource(share.serve)
        self.assertIn("start_network_refresh", source)

    def test_the_local_server_still_starts_it_too(self):
        self.assertIn("start_network_refresh",
                      inspect.getsource(server.serve))


class TestFollowingADevice(unittest.TestCase):
    """Opening the monitor while another Mac is selected must show that Mac."""

    def setUp(self):
        self.page = server.netmonitor_html()

    def test_the_page_reads_a_device_from_its_address(self):
        self.assertIn("device=", self.page)
        self.assertIn("var DEVICE", self.page)

    def test_its_reads_go_through_the_relay_when_one_is_named(self):
        self.assertIn("/api/remote?peer=", self.page)
        # and the two things it reads both go through that helper
        self.assertIn('fetch(api("/api/nettraffic"))', self.page)
        self.assertIn('fetch(api("/api/nethistory"', self.page)

    def test_another_machine_is_never_acted_on_from_here(self):
        # A device is somebody else's Mac; the off switch goes with it.
        self.assertIn("if (DEVICE) { READONLY = true; }", self.page)

    def test_the_dashboard_link_carries_the_device(self):
        dashboard = server.dashboard_html()
        self.assertIn("pointMonitorAtDevice", dashboard)
        self.assertIn('"/net?device=" + encodeURIComponent(DEVICE)', dashboard)

    def test_the_relay_will_carry_those_paths(self):
        # server._send_remote refuses a few paths outright; these must not be
        # among them, or the monitor would be relaying into a 400.
        source = inspect.getsource(server.Handler._send_remote)
        refused = source[source.index("not in ("):source.index("return self._send(400")]
        for path in ("/api/nettraffic", "/api/nethistory"):
            self.assertNotIn(path, refused, path)


class TestStillCannotAct(unittest.TestCase):
    """The whole point of the port, restated as a test."""

    def test_there_is_no_post_handler(self):
        self.assertFalse(hasattr(share.ShareHandler, "do_POST"))

    def test_the_paths_it_adds_are_all_reads(self):
        source = inspect.getsource(share.ShareHandler.do_GET)
        for acting in ("/api/kill", "/api/netblock", "/api/trash",
                       "/api/scan", "/api/cleanup", "/api/upgrade",
                       "/api/backup", "/api/export"):
            self.assertNotIn(acting, source, acting)

    def test_the_assets_it_serves_are_pictures_and_a_map(self):
        source = inspect.getsource(share.ShareHandler.do_GET)
        self.assertIn("/world.js", source)
        self.assertIn("/api/icon", source)


if __name__ == "__main__":
    unittest.main()
