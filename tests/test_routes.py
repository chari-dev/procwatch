import unittest

from procwatch import server


class TestRoutesExist(unittest.TestCase):
    """The handler must still be able to answer what it claims to.

    A refactor of do_GET deleted do_POST and do_OPTIONS outright -- everything
    between one method and the next went with it. The whole suite still
    passed, because nothing asserted the endpoints existed, and the symptom
    was that Quit returned 501 while every chart carried on working.
    """

    def test_it_answers_the_methods_it_needs(self):
        for method in ("do_GET", "do_POST", "do_OPTIONS"):
            self.assertTrue(hasattr(server.Handler, method),
                            "%s is missing from the request handler" % method)

    def test_the_mutating_routes_are_present(self):
        for name in ("_change_alert", "_change_peer", "_reject_cross_origin"):
            self.assertTrue(hasattr(server.Handler, name),
                            "%s is missing" % name)

    def test_the_read_routes_are_present(self):
        for name in ("_send_export", "_send_backup", "_send_remote",
                     "_serve_index"):
            self.assertTrue(hasattr(server.Handler, name), "%s is missing" % name)

    def test_every_mutating_route_goes_through_the_csrf_check(self):
        """The guard is what stands between a page on another origin and a
        dead process. A new POST route added without it would be a hole with
        no visible symptom."""
        import inspect
        source = inspect.getsource(server.Handler.do_POST)
        self.assertIn("_reject_cross_origin", source)
        # Every path do_POST accepts must be listed before that check runs.
        before = source[:source.index("_reject_cross_origin")]
        for path in ("/api/kill", "/api/alerts", "/api/peers"):
            self.assertIn(path, before,
                          "%s is handled but not behind the guard" % path)


class TestSharedApiSurface(unittest.TestCase):
    def test_api_get_covers_the_read_paths_the_dashboard_uses(self):
        # These are the paths the page and any peer ask for. One missing here
        # is a panel that never loads, locally or remotely.
        import inspect
        source = inspect.getsource(server.api_get)
        for path in ("/api/series", "/api/bucket", "/api/info", "/api/activity",
                     "/api/ports", "/api/now", "/api/live", "/api/storage",
                     "/api/alerts", "/api/search"):
            self.assertIn('"%s"' % path, source, "%s is not answerable" % path)


if __name__ == "__main__":
    unittest.main()
