# tests/test_server.py
"""Exercises the Handler directly, without server.serve()'s webbrowser.open()
or IDLE_TIMEOUT reaper -- those belong to the CLI, not to request handling.
"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from procwatch import config, db, server


class TestServer(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.dir, "t.db")
        patcher = mock.patch("procwatch.config.DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        conn = db.connect(self.db_path)
        db.init_schema(conn)
        with conn:
            conn.execute(
                "INSERT INTO sampler_state (pid, start_time, cputime_cs, updated_ts) "
                "VALUES (1, 0, 0, 1700000000)")
        conn.close()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.httpd.last_seen = 0
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

        def stop():
            self.httpd.shutdown()
            self.thread.join()

        self.addCleanup(stop)

    def _get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path)) as resp:
            return resp.status, json.loads(resp.read())

    def test_info_reports_the_last_tick_from_sampler_state(self):
        status, body = self._get("/api/info")
        self.assertEqual(status, 200)
        self.assertEqual(body["last_tick"], 1700000000)
        self.assertIn("hostname", body)
        self.assertIn("cores", body)
        self.assertIn("mem_total_kb", body)

    def test_series_defaults_to_the_last_day_and_returns_json(self):
        status, body = self._get("/api/series")
        self.assertEqual(status, 200)
        self.assertIn("system", body)
        self.assertIn("series", body)

    def test_bucket_without_params_is_a_bad_request(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/bucket")
        self.assertEqual(ctx.exception.code, 400)

    def test_root_serves_the_dashboard_html(self):
        with urllib.request.urlopen("http://127.0.0.1:%d/" % self.port) as resp:
            self.assertEqual(resp.status, 200)
            body = resp.read().decode()
        self.assertIn("<!doctype html>", body.lower())


if __name__ == "__main__":
    unittest.main()
