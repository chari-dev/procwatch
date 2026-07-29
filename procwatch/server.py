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
import subprocess
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import (alerts, archive, config, db, live, netstat, peers, procs,
               query, share, storage, system)

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


def api_get(conn, path, params):
    """Answer one read-only API path. Returns a JSON-able value, or None if
    the path is not one of ours.

    Split out of the request handler so the same answers can be produced
    without one. A peer is asked for its data by running this on that machine
    over SSH -- which means no peer has to open a port, and the reply is
    produced by exactly the code that would have served it locally rather than
    by a second implementation that can drift.
    """
    if path == "/api/series":
        # Through float first: a caller computing a window from pixels sends
        # fractional seconds, and refusing "1785290511.15" is pedantry about a
        # timestamp we understand perfectly well.
        end = _seconds(params.get("end", [time.time()])[0])
        start = _seconds(params.get("start", [end - 86400])[0])
        scope = params.get("scope", ["all"])[0]
        # Clamped rather than trusted: the cost of the query scales with the
        # count, and it arrives from a query string.
        limit = max(1, min(60, int(params.get("limit", [12])[0])))
        return query.series(conn, start, end, limit=limit, scope=scope)

    if path == "/api/bucket":
        tier = params.get("tier", [None])[0]
        ts = params.get("ts", [None])[0]
        if tier is None or ts is None:
            raise ValueError("tier and ts are required")
        return query.bucket_detail(conn, tier, _seconds(ts))

    if path == "/api/info":
        info = system.machine_info()
        # This machine's sharing key, so it can be read off the screen rather
        # than only from the terminal that started `share`. Local only: the
        # relay never reaches this branch for a peer, because a peer answers
        # /api/info with its own -- and a device's key is not something the
        # dashboard should be able to collect from the machines it watches.
        try:
            info["share_key"] = share.key(conn)
            info["share_host"] = share.local_address()
            info["share_port"] = share.DEFAULT_PORT
            # Present only when this Mac is on a private network. When it is,
            # it is the address to hand out: it works from anywhere and the
            # traffic is encrypted, which the local one is not.
            info["share_vpn"] = share.tailscale_address()
        except Exception:
            info["share_key"] = ""
            info["share_host"] = ""
        # This machine's own clock. Two Macs can disagree by hours -- these
        # two do -- so anything derived from a timestamp has to be worked out
        # against the clock that produced it. Without this a peer's "last
        # sample" age and every chart window are computed from the wrong one.
        info["now"] = int(time.time())
        info["last_tick"] = conn.execute(
            "SELECT MAX(updated_ts) FROM sampler_state").fetchone()[0]
        try:
            info["db_bytes"] = os.path.getsize(config.DB_PATH)
        except OSError:
            info["db_bytes"] = 0
        # The oldest sample anywhere, so a pan stops at the edge of what was
        # recorded rather than scrolling into a window that can only be empty.
        first = None
        for tier in config.TIERS:
            row = conn.execute(
                "SELECT MIN(ts) FROM sample_%s" % tier.name).fetchone()
            if row and row[0] is not None:
                first = row[0] if first is None else min(first, row[0])
        info["first_ts"] = first
        return info

    if path == "/api/activity":
        days = max(1, min(370, int(params.get("days", ["30"])[0])))
        now = int(time.time())
        return query.activity(conn, now - days * 86400, now)

    if path == "/api/ports":
        return netstat.listeners()

    if path == "/api/now":
        # Reads the machine directly and writes nothing.
        return live.snapshot()

    if path == "/api/live":
        return procs.live_tree()

    if path == "/api/storage":
        return storage.usage(conn, limit=30)

    if path == "/api/alerts":
        return {"rules": alerts.rules(conn),
                "events": alerts.recent(conn, limit=40),
                "metrics": alerts.METRICS}

    if path == "/api/search":
        return query.search(conn, params.get("q", [""])[0], limit=25)

    return None


def _csv(text):
    """One CSV field.

    Process names contain commas ("Arc Helper (Renderer), v2") and quotes, and
    a spreadsheet reading an unescaped one silently shifts every column after
    it -- which looks like data rather than damage.
    """
    text = "" if text is None else str(text)
    if any(c in text for c in ',"\n\r'):
        return '"' + text.replace('"', '""') + '"'
    return text


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
        if parsed.path in ("/", "/index.html"):
            return self._serve_index()
        if parsed.path == "/api/peers":
            return self._send(200, json.dumps(peers.listing()), "application/json")
        if parsed.path == "/api/remote":
            return self._send_remote(params)
        conn = db.connect(config.DB_PATH)
        try:
            if parsed.path == "/api/export":
                return self._send_export(conn, params)
            if parsed.path == "/api/backup":
                return self._send_backup()
            payload = api_get(conn, parsed.path, params)
            if payload is None:
                return self._send(404, "not found", "text/plain")
            self._send(200, json.dumps(payload), "application/json")
        except ValueError as exc:
            # Bad int params (start/end/ts) and query.bucket_detail's unknown-
            # tier guard both land here; both are the caller's fault.
            self._send(400, str(exc), "text/plain")
        finally:
            conn.close()

    def do_POST(self):
        """The mutating endpoints: signal a process, and change alert rules.

        Restricted to processes the calling user owns, which is also all the
        kernel would permit without privilege. The check is here so the
        failure is a legible message rather than a permission error, and so a
        typo cannot aim a signal at a system daemon by accident.
        """
        self.server.last_seen = time.time()
        parsed = urlparse(self.path)
        if parsed.path not in ("/api/kill", "/api/alerts", "/api/peers"):
            return self._send(404, "not found", "text/plain")
        # Same guard for both. Changing a rule is far less dangerous than
        # signalling a process, but a page on another origin still has no
        # business silencing someone's alerts.
        refusal = self._reject_cross_origin()
        if refusal:
            return self._send(403, json.dumps({"ok": False, "error": refusal}),
                              "application/json")
        if parsed.path == "/api/alerts":
            return self._change_alert()
        if parsed.path == "/api/peers":
            return self._change_peer()
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

    def _change_alert(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, json.dumps({"error": "malformed request"}),
                              "application/json")
        conn = db.connect(config.DB_PATH)
        try:
            action = body.get("action")
            if action == "add":
                alerts.add(conn, str(body.get("pattern") or "*"),
                           str(body.get("metric") or "cpu"),
                           float(body.get("threshold", 80)),
                           int(body.get("sustain", 600)))
            elif action == "remove":
                alerts.remove(conn, int(body["id"]))
            else:
                return self._send(400, json.dumps({"error": "unknown action"}),
                                  "application/json")
            return self._send(200, json.dumps({"ok": True,
                                               "rules": alerts.rules(conn)}),
                              "application/json")
        except (ValueError, KeyError, TypeError) as error:
            return self._send(400, json.dumps({"error": str(error)}),
                              "application/json")
        finally:
            conn.close()

    def do_OPTIONS(self):
        """Refuse preflight explicitly.

        Answering it with permissive CORS headers is what would let a hostile
        page through; declining is the whole point, so it is stated here
        rather than left to the base class's 501.
        """
        self._send(403, "cross-origin requests are not served", "text/plain")

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

    def _change_peer(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, json.dumps({"error": "malformed request"}),
                              "application/json")
        try:
            action = body.get("action")
            if action == "add":
                peers.add(str(body.get("name") or ""),
                          str(body.get("host") or ""),
                          str(body.get("key") or ""))
            elif action == "remove":
                peers.remove(str(body.get("name") or ""))
            else:
                return self._send(400, json.dumps({"error": "unknown action"}),
                                  "application/json")
        except (ValueError, KeyError, TypeError) as error:
            return self._send(400, json.dumps({"error": str(error)}),
                              "application/json")
        self._send(200, json.dumps({"ok": True, "peers": peers.listing()}),
                   "application/json")

    def _send_remote(self, params):
        """Relay one read-only API path to a peer over SSH.

        Only the read side: there is no remote kill, and there is not going to
        be one behind a proxy the browser can reach. Ending a process on
        another machine deserves to be a decision made on that machine.

        The path is checked against the ones this server itself answers, so a
        crafted value cannot become an argument to something else on the far
        side, and only the peer's name comes from the caller -- the host and
        the program path come from the local peer list.
        """
        name = params.get("peer", [""])[0]
        path = params.get("path", ["/api/info"])[0]
        if not path.startswith("/api/") or path in ("/api/remote", "/api/peers",
                                                    "/api/backup", "/api/export"):
            return self._send(400, json.dumps({"error": "not a relayable path"}),
                              "application/json")
        forwarded = {k: v for k, v in params.items()
                     if k not in ("peer", "path")}
        try:
            payload = peers.fetch(name, path, forwarded)
        except KeyError:
            return self._send(404, json.dumps({"error": "no such device"}),
                              "application/json")
        except (RuntimeError, OSError) as error:
            return self._send(502, json.dumps({"error": str(error)[:300]}),
                              "application/json")
        except subprocess.TimeoutExpired:
            return self._send(504, json.dumps(
                {"error": "%s did not answer in time" % name}),
                "application/json")
        self._send(200, json.dumps(payload), "application/json")

    def _send_export(self, conn, params):
        """The window on screen as CSV.

        The backup is the whole database in SQLite's own format, which is the
        right thing for keeping and the wrong thing for a spreadsheet. This is
        one row per process per bucket over one window -- what you would paste
        into a spreadsheet to ask a question this dashboard does not answer.
        """
        end = _seconds(params.get("end", [time.time()])[0])
        start = _seconds(params.get("start", [end - 86400])[0])
        scope = params.get("scope", ["all"])[0]
        limit = max(1, min(60, int(params.get("limit", [12])[0])))
        data = query.series(conn, start, end, limit=limit, scope=scope)
        lines = ["time,process,application,cpu_avg_percent,cpu_max_percent,"
                 "memory_bytes,processes,net_in_bytes,net_out_bytes,"
                 "disk_read_bytes,disk_write_bytes"]
        for series in data["series"]:
            name = series["exe"]
            app = series.get("app", "")
            for point in series["points"]:
                lines.append(",".join([
                    time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(point["ts"])),
                    _csv(name), _csv(app),
                    "%.2f" % point["cpu_avg"], "%.2f" % point["cpu_max"],
                    "%d" % int(point["rss_avg"] * 1024),
                    "%d" % point["nproc"],
                    "%d" % point["net_in"], "%d" % point["net_out"],
                    "%d" % point["disk_read"], "%d" % point["disk_write"]]))
        body = ("\n".join(lines) + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition",
                         'attachment; filename="procwatch-%s.csv"'
                         % time.strftime("%Y-%m-%d-%H%M", time.localtime(end)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

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
