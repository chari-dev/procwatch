"""On-demand local dashboard server. Exits when idle.

Started by `procwatch open`, not by launchd -- this is a diagnostic tool a
person runs while looking at the machine, not a resident daemon. It binds
127.0.0.1 only and shuts itself down after IDLE_TIMEOUT seconds of no
requests.
"""
import hmac
import json
import os
import secrets
import sqlite3
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import archive, config, db, live, netstat, procs, query, system

IDLE_TIMEOUT = 900
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _token_of(server):
    """This run's CSRF token, generated on first use.

    serve() sets it up front; a server constructed directly (tests, or an
    embedding caller) gets one lazily rather than an AttributeError.
    """
    token = getattr(server, "token", None)
    if token is None:
        token = secrets.token_urlsafe(24)
        server.token = token
    return token


def _seconds(value):
    """A whole-second timestamp from a query parameter.

    Accepts "1785290511" and "1785290511.153447" alike; still raises
    ValueError on anything that is not a number, which the caller turns into
    a 400 rather than guessing.
    """
    return int(float(value))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, content_type):
        payload = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        # Nothing here is ever worth caching: the API is live data and the
        # page changes whenever the dashboard is edited. Without this a
        # reload can serve a stale page indefinitely, which looks exactly
        # like a broken build.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.server.last_seen = time.time()
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        conn = db.connect(config.DB_PATH)
        try:
            if parsed.path in ("/", "/index.html"):
                return self._serve_index()
            if parsed.path == "/api/series":
                # Through float first: a caller computing a window from pixels
                # sends fractional seconds, and refusing "1785290511.15" as
                # unparseable is pedantry about a timestamp we understand
                # perfectly well.
                end = _seconds(params.get("end", [time.time()])[0])
                start = _seconds(params.get("start", [end - 86400])[0])
                scope = params.get("scope", ["all"])[0]
                # Clamped rather than trusted: the cost of this query scales
                # with the count, and it arrives from a query string.
                limit = max(1, min(60, int(params.get("limit", [12])[0])))
                return self._send(200, json.dumps(
                    query.series(conn, start, end, limit=limit, scope=scope)),
                    "application/json")
            if parsed.path == "/api/bucket":
                tier = params.get("tier", [None])[0]
                ts = params.get("ts", [None])[0]
                if tier is None or ts is None:
                    return self._send(400, "tier and ts are required", "text/plain")
                return self._send(200, json.dumps(
                    query.bucket_detail(conn, tier, _seconds(ts))),
                    "application/json")
            if parsed.path == "/api/info":
                info = system.machine_info()
                last = conn.execute(
                    "SELECT MAX(updated_ts) FROM sampler_state").fetchone()[0]
                info["last_tick"] = last
                # Settings offers a backup, and how big the download will be
                # is the first thing anyone wants to know before clicking it.
                try:
                    info["db_bytes"] = os.path.getsize(config.DB_PATH)
                except OSError:
                    info["db_bytes"] = 0
                # The oldest sample anywhere, so the dashboard can stop a pan
                # at the edge of what was recorded instead of scrolling into
                # a window that can only ever be empty.
                first = None
                for tier in config.TIERS:
                    row = conn.execute(
                        "SELECT MIN(ts) FROM sample_%s" % tier.name).fetchone()
                    if row and row[0] is not None:
                        first = row[0] if first is None else min(first, row[0])
                info["first_ts"] = first
                return self._send(200, json.dumps(info), "application/json")
            if parsed.path == "/api/activity":
                days = max(1, min(370, int(params.get("days", ["30"])[0])))
                end_ts = int(time.time())
                return self._send(200, json.dumps(query.activity(
                    conn, end_ts - days * 86400, end_ts)), "application/json")
            if parsed.path == "/api/ports":
                return self._send(200, json.dumps(netstat.listeners()),
                                  "application/json")
            if parsed.path == "/api/now":
                # Reads the machine directly and writes nothing. The recorder
                # keeps its 30s cadence for history; this is for watching.
                return self._send(200, json.dumps(live.snapshot()),
                                  "application/json")
            if parsed.path == "/api/live":
                return self._send(200, json.dumps(procs.live_tree()),
                                  "application/json")
            if parsed.path == "/api/backup":
                return self._send_backup()
            self._send(404, "not found", "text/plain")
        except ValueError as exc:
            # Bad int params (start/end/ts) and query.bucket_detail's unknown-
            # tier guard both land here -- a malformed request, not a server
            # fault.
            self._send(400, str(exc), "text/plain")
        finally:
            conn.close()

    def _reject_cross_origin(self):
        """Return an error string if this request could be a forged one.

        Binding 127.0.0.1 keeps other machines out; it does nothing about the
        browser already on this one. Any page the user visits can POST to
        localhost, and with Content-Type text/plain that is a CORS "simple
        request" -- no preflight, sent straight through. The attacker cannot
        read the reply, but a process is already dead by then. Verified
        against this very endpoint before these checks existed.

        Four layers, because each covers a different escape:
          - a JSON Content-Type is not a simple request, so it forces a
            preflight the server never answers;
          - a custom header cannot be set cross-origin without that same
            preflight;
          - Origin, when the browser sends it, must be our own;
          - a token minted per server run and readable only by same-origin
            script, so a blind cross-origin POST cannot guess it.
        """
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype != "application/json":
            return "Content-Type must be application/json"
        if self.headers.get("X-Procwatch") != "1":
            return "missing X-Procwatch header"
        origin = self.headers.get("Origin")
        allowed = getattr(self.server, "allowed_origins", None)
        if origin and allowed is not None and origin not in allowed:
            return "cross-origin request refused"
        token = self.headers.get("X-Procwatch-Token")
        if not token or not hmac.compare_digest(token, _token_of(self.server)):
            return "bad or missing session token"
        return None

    def do_POST(self):
        """The only mutating endpoint: signal a process.

        Restricted to processes the calling user owns, which is also all the
        kernel would permit without privilege. The check is here so the
        failure is a legible message rather than a permission error, and so a
        typo cannot aim a signal at a system daemon by accident.
        """
        self.server.last_seen = time.time()
        parsed = urlparse(self.path)
        if parsed.path != "/api/kill":
            return self._send(404, "not found", "text/plain")
        refusal = self._reject_cross_origin()
        if refusal:
            return self._send(403, json.dumps({"ok": False, "error": refusal}),
                              "application/json")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            pid = int(body["pid"])
            signal_name = body.get("signal", "TERM")
        except (ValueError, KeyError, TypeError):
            return self._send(400, json.dumps({"ok": False, "error": "pid required"}),
                              "application/json")
        result = procs.signal_pid(pid, signal_name)
        code = 200 if result["ok"] else 400
        self._send(code, json.dumps(result), "application/json")

    def do_OPTIONS(self):
        """Refuse preflight explicitly.

        Answering it with permissive CORS headers is what would let a hostile
        page through; declining is the whole point, so it is stated here
        rather than left to the base class's 501.
        """
        self._send(403, "cross-origin requests are not served", "text/plain")

    def _send_backup(self):
        """A consistent snapshot of the database, as a download.

        Written to a temporary file first rather than assembled in memory: a
        year of history is a few hundred megabytes, and the point of a backup
        is that it works when things are already going badly.

        Read-only and idempotent, so it belongs on GET -- but for that same
        reason it is not behind the CSRF check that guards /api/kill, and a
        page on another origin could point a user's browser at it. It can only
        ever return this user's own history to this user's own machine, which
        is what the dashboard already shows them.
        """
        handle, temp = tempfile.mkstemp(prefix="procwatch-backup-", suffix=".db")
        os.close(handle)
        try:
            archive.backup(temp)
            with open(temp, "rb") as source:
                payload = source.read()
        except (RuntimeError, OSError, sqlite3.Error) as error:
            return self._send(500, "backup failed: %s" % error, "text/plain")
        finally:
            for leftover in (temp, temp + "-wal", temp + "-shm"):
                if os.path.exists(leftover):
                    os.remove(leftover)
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.sqlite3")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % archive.default_name())
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(payload)

    def _serve_index(self):
        """Serve the page with this run's token embedded.

        Same-origin script can read it; a cross-origin page cannot read this
        response at all, which is what makes the token worth anything.
        """
        try:
            with open(os.path.join(STATIC, "index.html"), "r") as handle:
                page = handle.read()
        except FileNotFoundError:
            return self._send(404, "no dashboard installed", "text/plain")
        page = page.replace("__PROCWATCH_TOKEN__", _token_of(self.server))
        self._send(200, page.encode(), "text/html; charset=utf-8")


def serve(port, open_browser=True, idle_timeout=IDLE_TIMEOUT):
    if not os.path.exists(config.DB_PATH):
        print("no database yet; run `procwatch install` and wait a minute")
        return 1
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.last_seen = time.time()
    # Minted per run, so a token lifted from one session is useless against
    # the next, and nothing needs to be stored on disk.
    httpd.token = secrets.token_urlsafe(24)
    httpd.allowed_origins = {"http://127.0.0.1:%d" % port,
                             "http://localhost:%d" % port}

    def reap():
        while time.time() - httpd.last_seen < idle_timeout:
            time.sleep(30)
        httpd.shutdown()

    # idle_timeout=None keeps it up indefinitely, which is what the menu bar
    # app wants: it owns the lifetime and quitting it stops the server.
    if idle_timeout is not None:
        threading.Thread(target=reap, daemon=True).start()
    live.start_network_refresh()
    url = "http://127.0.0.1:%d/" % port
    print("serving %s%s" % (url, "  (exits after %d minutes idle)" % (idle_timeout // 60)
                            if idle_timeout else ""))
    if open_browser:
        webbrowser.open(url)
    httpd.serve_forever()
    return 0
