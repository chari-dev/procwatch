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

from . import (alerts, archive, battery, config, db, diagnose, events, icons,
               knowledge, live, netpeer, netstat, peers, power, prefs, procs,
               query, selfupdate, share, space, storage, system, versions)

IDLE_TIMEOUT = 900
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


# The state of a scan in progress. One at a time: a second walk of the same
# disk would halve the speed of the first and answer the same question.
_SCAN = {"running": False, "progress": None, "error": ""}


def _start_scan(root=None):
    """Begin a scan in the background, if one is not already going."""
    if _SCAN.get("running"):
        return False
    _SCAN.update({"running": True, "progress": {"files": 0, "bytes": 0},
                  "error": ""})

    def work():
        conn = db.connect(config.DB_PATH)
        try:
            space.scan(conn, root,
                       progress=lambda files, size: _SCAN.__setitem__(
                           "progress", {"files": files, "bytes": size}))
        except Exception as problem:
            _SCAN["error"] = str(problem)
        finally:
            conn.close()
            _SCAN["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return True


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
        out = query.series(conn, start, end, limit=limit, scope=scope)
        # A second ranking of the same window, by energy rather than by CPU
        # peak, for the battery chart. Carried in the same response because the
        # buckets have to line up with the battery readings beside them, and
        # two requests can straddle a tick and disagree about the last one.
        if params.get("energy", ["0"])[0] not in ("0", "", "false"):
            out["energy_series"] = query.series(
                conn, start, end, limit=min(limit, 10), scope=scope,
                rank="energy")["series"]
        return out

    if path == "/api/bucket":
        tier = params.get("tier", [None])[0]
        ts = params.get("ts", [None])[0]
        if tier is None or ts is None:
            raise ValueError("tier and ts are required")
        rows = query.bucket_detail(conn, tier, _seconds(ts))
        return _attach_live_pids(rows)

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
        days = max(1, min(400, int(params.get("days", ["30"])[0])))
        now = int(time.time())
        # One cell per day for the year grid, per hour for anything shorter.
        # A year of hourly rows is 8,760 of them, all but 365 of which the grid
        # would add together and discard.
        if params.get("by", ["hour"])[0] == "day":
            return query.activity_days(conn, now - days * 86400, now)
        return query.activity(conn, now - days * 86400, now)

    if path == "/api/ports":
        return netstat.listeners()

    if path == "/api/now":
        # Reads the machine directly and writes nothing.
        return live.snapshot()

    if path == "/api/live":
        return procs.live_tree()

    if path == "/api/nethistory":
        # The network monitor, looking backwards. The recording keeps bytes
        # per process per bucket, so a past window can say who was talking
        # and how much -- but not to whom: peers are only known while a
        # connection is open, and are never written down.
        end = _seconds(params.get("end", [time.time()])[0])
        start = _seconds(params.get("start", [end - 3600])[0])
        limit = max(1, min(40, int(params.get("limit", ["18"])[0])))
        data = query.series(conn, start, end, limit=limit, rank="net")
        # Folded by the name shown, because one application is often several
        # recorded identities -- a browser and its renderers -- and a list
        # naming the same program three times is a list nobody can read.
        merged = {}
        for entry in data["series"]:
            name = entry.get("app") or entry["exe"]
            if entry["exe"] == config.OTHER:
                name = "Everything else"
            row = merged.setdefault(name, {
                "app": name, "is_system": entry.get("is_system", False),
                "bytes_in": 0, "bytes_out": 0, "by_ts": {}})
            for point in entry["points"]:
                row["bytes_in"] += point["net_in"]
                row["bytes_out"] += point["net_out"]
                slot = row["by_ts"].setdefault(point["ts"], [0, 0])
                slot[0] += point["net_in"]
                slot[1] += point["net_out"]
        apps = []
        for row in merged.values():
            if not row["bytes_in"] and not row["bytes_out"]:
                continue
            row["points"] = [{"ts": ts, "in": v[0], "out": v[1]}
                             for ts, v in sorted(row.pop("by_ts").items())]
            apps.append(row)
        apps.sort(key=lambda a: -(a["bytes_in"] + a["bytes_out"]))
        return {"apps": apps, "start": start, "end": end}

    if path == "/api/netmap":
        # The map, asked about a time rather than about now. Everything here
        # is read from the recorded peer history, so a window from six hours
        # ago answers exactly as well as the last five minutes -- which is the
        # whole point of keeping it. `span` tells the scrubber how far back it
        # is allowed to drag; without it the control invents a range the
        # database cannot fill and every drag past the edge looks broken.
        end = _seconds(params.get("end", [time.time()])[0])
        start = _seconds(params.get("start", [end - 3600])[0])
        held = netpeer.span(conn)
        # The peers are the chosen window. The timeline is deliberately NOT:
        # it is the scrubber's own track, and a track that only covers where
        # you already are is blank everywhere you have not been. Drawn across
        # the full scrubbable range it does what it exists for -- showing
        # where the traffic is before you drag to it -- and it no longer has
        # to be refetched mid-drag, since dragging does not change it.
        track_start = held["first"] if held else start
        track_end = max(end, held["last"] if held else end)
        return {"start": start, "end": end,
                "peers": netpeer.peers(conn, start, end),
                "span": held,
                "timeline": netpeer.timeline(conn, track_start, track_end)}

    if path == "/api/nettraffic":
        # Who is talking to the network, to whom, at what rate. Served from
        # the background nettop pass, so it never blocks on a five-second
        # tool; the answer says how old it is.
        return live.network_traffic()

    if path == "/api/battery":
        # Condition now, plus the charge history the sampler already keeps, so
        # the page can say both "85% of new" and "this is what today did".
        state = battery.read()
        hours = max(1, min(720, int(params.get("hours", ["24"])[0])))
        rows = conn.execute(
            "SELECT ts, batt_pct, batt_draw_mw, on_ac FROM system_raw "
            "WHERE ts >= ? AND batt_pct >= 0 ORDER BY ts",
            (int(time.time()) - hours * 3600,)).fetchall()
        return {"now": state, "verdict": battery.verdict(state),
                "history": [{"ts": r[0], "percent": r[1],
                             "draw_mw": r[2] if r[2] and r[2] > 0 else None,
                             "on_ac": bool(r[3])} for r in rows]}

    if path == "/api/storage":
        return storage.usage(conn, limit=30)

    if path == "/api/alerts":
        return {"rules": alerts.rules(conn),
                "events": alerts.recent(conn, limit=40),
                "metrics": alerts.METRICS}

    if path == "/api/search":
        return query.search(conn, params.get("q", [""])[0], limit=25)

    if path == "/api/what":
        # What one process is. Answered from the catalogue and from this
        # machine's own history together, so "is this normal" is answered with
        # what is normal *here* rather than only in general.
        name = params.get("name", [""])[0]
        entry = knowledge.describe(name, params.get("cmdline", [""])[0],
                                  params.get("app", [""])[0])
        entry["usual"] = query.usual(conn, name)
        return entry

    if path == "/api/events":
        # The history, digested. Defaults to a month, which is the span over
        # which a repeat becomes visible -- a day of events is a log, and a
        # month of them is a pattern.
        end = _seconds(params.get("end", [time.time()])[0])
        days = max(1, min(400, int(params.get("days", ["30"])[0])))
        start = _seconds(params.get("start", [end - days * 86400])[0])
        digest = events.digest(conn, start, end)
        digest["timeline"] = events.timeline(conn, start, end, limit=120)
        for pattern in digest["patterns"]:
            pattern["says"] = events.describe_pattern(pattern, now=int(end))
        return digest

    if path == "/api/prefs":
        return prefs.all_prefs(conn)

    if path == "/api/space":
        # Whatever is known right now. The scan itself takes minutes, so this
        # never starts one: it reports the last one and whether another is
        # running, and the page decides what to show.
        found = space.latest(conn)
        out = {"volume": space.volumes(), "snapshots": space.snapshots(),
               "scan": found, "running": _SCAN.get("running", False),
               "progress": _SCAN.get("progress"),
               "error": _SCAN.get("error", ""),
               "reconcile": space.reconcile(conn),
               "blocked": space.guarded()}
        if found and found["finished_ts"]:
            sid = found["id"]
            under = params.get("under", [None])[0]
            out["kinds"] = space.kinds(conn, sid)
            out["owners"] = space.owners(conn, sid)
            # Scoped with the folder view: looking inside ~/Movies should
            # rank the files there, not repeat the same global fifteen.
            out["files"] = space.biggest_files(conn, sid, under=under)
            # Standing in a folder is a different question from surveying a
            # disk. The recorded scan keeps only files over its floor, so a
            # folder full of 20 MB files reads as empty; reading that one
            # directory now costs a single readdir and answers what is
            # actually in front of you. Only when a folder is named -- the
            # top level is the survey, and that is what the scan is for.
            # NOT named `live`: that is the module this function calls for
            # /api/now, and a local of the same name makes Python treat the
            # name as local for the whole function -- so binding it here left
            # `live.snapshot()` raising UnboundLocalError on a completely
            # different endpoint, which is a fault with no visible connection
            # to the line that caused it.
            if under:
                here = space.files_in(under)
                if here:
                    out["files"] = here
            out["dirs"] = space.biggest_dirs(conn, sid, under=under)
            out["under"] = under or found["root"]
            out["apps"] = space.applications(conn, sid)
            for row in out["dirs"]:
                row["about"] = space.explain(row["path"])
        return out

    if path == "/api/app":
        # Everything one application put outside its own bundle. Read-only:
        # the list is meant to be looked at before anything happens to it.
        found = space.leftovers(params.get("path", [""])[0])
        if not found:
            raise ValueError("not an application bundle")
        found["running"] = space.running(found["app"]["path"])
        return found

    if path == "/api/caches":
        return {"caches": space.caches()}

    if path == "/api/badge":
        # What the menu bar shows. Cheap enough to poll: the verdict over an
        # hour, which is the same work the dashboard does when it opens.
        count, keys = diagnose.unread(conn)
        out = {"count": count, "keys": keys,
               "enabled": prefs.findings_on(conn)}
        # notes=1 is the menu bar app collecting the notification queue to
        # post as itself. Collected is delivered: handing the same note out
        # twice is a notification that appears twice. Local-only like the
        # rest, and the worst a hostile page can do with it is suppress a
        # notification it cannot read.
        if params.get("notes", ["0"])[0] not in ("0", "", "false"):
            out["notes"] = alerts.pending(conn, claim=True)
        return out

    if path == "/api/why":
        # The verdict for a window. Defaults to the last hour, which is the
        # span someone has in mind when they say "it was slow just now".
        end = _seconds(params.get("end", [time.time()])[0])
        start = _seconds(params.get("start", [end - 3600])[0])
        return diagnose.explain(conn, start, end)

    if path == "/api/power":
        end = _seconds(params.get("end", [time.time()])[0])
        start = _seconds(params.get("start", [end - 86400])[0])
        return {"holding_now": power.holding_now(conn),
                "kept_awake": power.kept_awake(conn, start, end),
                "spans": power.nights(conn, start, end)[-40:],
                "drain": power.overnight_drain(conn, start, end)}

    if path == "/api/growth":
        days = max(1, min(370, int(params.get("days", ["7"])[0])))
        now = int(time.time())
        return storage.growth(conn, since=now - days * 86400, now=now)

    if path == "/api/upgrade":
        # Whether a newer procwatch exists on GitHub. Answered by this server
        # rather than the page because the page cannot ask another origin --
        # and cached inside selfupdate, so an open dashboard does not poll
        # GitHub.
        force = params.get("force", ["0"])[0] not in ("0", "", "false")
        return selfupdate.check(force=force)

    if path == "/api/cleanup":
        # What one press of Clean up would move to the Trash, itemised and
        # totalled. Read-only: the press itself arrives as a POST.
        return space.cleanup_plan()

    if path == "/api/updates":
        days = max(1, min(370, int(params.get("days", ["30"])[0])))
        now = int(time.time())
        # Annotated rather than bare: the panel has to name the update and say
        # what happened to it, including "not enough recorded yet", which a list
        # of regressions alone cannot express.
        return {"history": versions.history(conn, now=now),
                "updates": versions.compared(conn, since=now - days * 86400,
                                            now=now)[:40],
                "regressions": versions.regressions(conn,
                                                    since=now - days * 86400,
                                                    now=now)}

    return None


def _attach_live_pids(rows, by_identity=None):
    """Say which of these historical rows are still running, and as what.

    The drill-down is a picture of a past minute, and the question it provokes
    -- "is that thing still doing it, and can I stop it" -- has no answer in the
    history itself. So each row is matched against what is running right now.

    Matched by identity rather than by a recorded PID, which is not stored and
    would be the wrong thing to use if it were: PIDs are reused within hours, so
    a button aimed at "the process from 4:15pm" would eventually hit something
    unrelated while still carrying its name.
    """
    if by_identity is None:
        try:
            by_identity, _ = procs.running_now()
        except Exception:
            # A failed `ps` costs the buttons, not the panel.
            return rows
    for row in rows:
        if row.get("is_other"):
            row["pids"] = []
            continue
        # Keyed on what the sampler stored, not on a re-derivation of the
        # command line -- that disagreed on a quarter of the rows here, and a
        # near miss fell through to "everything in this application", which
        # would have put a Quit button that ends all of Arc on a row naming one
        # of its renderers.
        key = (row["exe"], row.get("args_sig") or "")
        row["pids"] = sorted(by_identity.get(key, []))
    return rows


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


def world_js():
    """The country outlines, as generated data. Replaced by the bundle."""
    with open(os.path.join(STATIC, "world.js"), "r") as handle:
        return handle.read()


def netmonitor_html():
    """The network monitor page, unrendered.

    A separate page rather than a card: it is an instrument someone leaves
    open, and it earns a window of its own. Same single-source rule as the
    dashboard -- the bundle replaces this with an embedded copy.
    """
    with open(os.path.join(STATIC, "netmonitor.html"), "r") as handle:
        return handle.read()


def battery_html():
    """The battery page, unrendered. Same single-source rule as the others."""
    with open(os.path.join(STATIC, "battery.html"), "r") as handle:
        return handle.read()


def storage_html():
    """The storage page, unrendered.

    Same reasoning as the network monitor: working out where a disk went is a
    thing you go and do, not a card you glance at, and it was previously a
    full-screen sheet over a dashboard that kept refreshing behind it.
    """
    with open(os.path.join(STATIC, "storage.html"), "r") as handle:
        return handle.read()


def dashboard_html():
    """The dashboard page, unrendered.

    Both servers need it -- the local one and the read-only sharing port --
    and the single-file build replaces this function with one that returns an
    embedded copy. Keeping the lookup in one place is what stops the shared
    port from drifting into a second, older dashboard.
    """
    with open(os.path.join(STATIC, "index.html"), "r") as handle:
        return handle.read()


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
        if parsed.path == "/api/icon":
            # An application's own icon. Cached hard: a bundle's icon does
            # not change until the application is updated, and the cache key
            # already carries that.
            # By path where the caller knows one, by name where it only has
            # the name -- which is most of the dashboard.
            where = params.get("path", [""])[0] or \
                icons.bundle_for_name(params.get("app", [""])[0])
            body = icons.png(where, int(params.get("size", ["64"])[0] or 64))
            if body is None:
                return self._send(404, "no icon", "text/plain")
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=604800")
            self.end_headers()
            return self.wfile.write(body)
        if parsed.path == "/world.js":
            # The country outlines the globe is drawn from. Static, and the
            # one thing here big enough that re-sending it every poll would
            # be silly -- so this is the only response allowed to be cached.
            try:
                body = world_js().encode()
            except OSError:
                return self._send(404, "no world data", "text/plain")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            return self.wfile.write(body)
        if parsed.path == "/net":
            # Carries this run's token like the dashboard: the monitor's off
            # switch is a POST, and it goes through the same guard.
            try:
                page = netmonitor_html()
            except OSError:
                return self._send(404, "no network monitor installed",
                                  "text/plain")
            page = page.replace("__PROCWATCH_TOKEN__", _token_of(self.server))
            return self._send(200, page.encode(), "text/html; charset=utf-8")
        if parsed.path == "/battery":
            try:
                page = battery_html()
            except OSError:
                return self._send(404, "no battery page installed", "text/plain")
            page = page.replace("__PROCWATCH_TOKEN__", _token_of(self.server))
            return self._send(200, page.encode(), "text/html; charset=utf-8")
        if parsed.path == "/disk":
            # Same shape as /net: its own page, carrying this run's token
            # because starting a scan is a POST and goes through the guard.
            try:
                page = storage_html()
            except OSError:
                return self._send(404, "no storage page installed", "text/plain")
            page = page.replace("__PROCWATCH_TOKEN__", _token_of(self.server))
            return self._send(200, page.encode(), "text/html; charset=utf-8")
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
        if parsed.path not in ("/api/kill", "/api/alerts", "/api/peers",
                               "/api/prefs", "/api/read", "/api/scan",
                               "/api/trash", "/api/settings", "/api/upgrade",
                               "/api/cleanup", "/api/netblock", "/api/reveal"):
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
        if parsed.path == "/api/prefs":
            return self._change_prefs()
        if parsed.path == "/api/read":
            return self._mark_read()
        if parsed.path == "/api/scan":
            started = _start_scan()
            return self._send(200, json.dumps(
                {"ok": True, "started": started, "running": _SCAN["running"]}),
                "application/json")
        if parsed.path == "/api/trash":
            return self._trash()
        if parsed.path == "/api/reveal":
            return self._reveal()
        if parsed.path == "/api/upgrade":
            # Install the newer version. The reply says what happened and
            # whether a restart is owed; it never restarts anything itself,
            # because this handler is the thing that would be restarted.
            result = selfupdate.apply()
            return self._send(200 if result["ok"] else 500,
                              json.dumps(result), "application/json")
        if parsed.path == "/api/cleanup":
            return self._cleanup()
        if parsed.path == "/api/netblock":
            return self._netblock()
        if parsed.path == "/api/settings":
            # Open the Full Disk Access pane. The dashboard runs in a web view
            # that cannot follow an x-apple.systempreferences: link itself, and
            # telling somebody to navigate four levels of System Settings is how
            # a permission never gets granted.
            subprocess.run(
                ["open", "x-apple.systempreferences:com.apple.preference."
                         "security?Privacy_AllFiles"],
                capture_output=True, timeout=15)
            return self._send(200, json.dumps({"ok": True}), "application/json")
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

    def _change_prefs(self):
        """Change a setting the recorder reads.

        Only the keys prefs knows, and only the values it allows -- the
        validation is there rather than here so the CLI and this cannot disagree
        about what a legal setting is.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, json.dumps({"error": "malformed request"}),
                              "application/json")
        conn = db.connect(config.DB_PATH)
        try:
            was = prefs.findings_on(conn)
            for key, value in body.items():
                prefs.set(conn, str(key), value)
            # Switching the diagnosis off forgets what it has already mentioned,
            # so switching it on again next month starts quiet rather than
            # comparing against a table from before.
            if was and not prefs.findings_on(conn):
                diagnose.forget_findings(conn)
            return self._send(200, json.dumps({"ok": True,
                                               "prefs": prefs.all_prefs(conn)}),
                              "application/json")
        except (KeyError, ValueError, TypeError) as error:
            return self._send(400, json.dumps({"error": str(error)}),
                              "application/json")
        finally:
            conn.close()

    def _mark_read(self):
        """Note that the findings have been looked at.

        Sent when the verdict is opened. The keys are taken from the request
        where the caller supplied them, so anything that turned up between the
        panel being drawn and this arriving is still counted as unread rather
        than silently marked as seen.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        conn = db.connect(config.DB_PATH)
        try:
            keys = body.get("keys")
            read = diagnose.mark_read(conn, keys if keys else None)
            return self._send(200, json.dumps({"ok": True, "read": read,
                                               "count": diagnose.unread(conn)[0]}),
                              "application/json")
        finally:
            conn.close()

    def _trash(self):
        """Move what was asked for to the Trash.

        The refusals live in space.trash rather than here, so the CLI and this
        cannot disagree about what may be deleted -- and the answer says what
        happened to each path rather than one overall success.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, json.dumps({"error": "malformed request"}),
                              "application/json")
        paths = body.get("paths") or []
        if not isinstance(paths, list) or not paths:
            return self._send(400, json.dumps({"error": "no paths given"}),
                              "application/json")
        results = space.trash([str(p) for p in paths])
        freed = sum(1 for r in results if r["ok"])
        return self._send(200, json.dumps({"ok": True, "results": results,
                                           "moved": freed}),
                          "application/json")

    def _reveal(self):
        """Show a path in Finder.

        `open -R` selects the item in its enclosing folder rather than opening
        it, which is what "show me this" means and, more to the point, is not
        "run this". A page that could ask for `open` on an arbitrary path
        could ask for it on an executable.

        The path is resolved and checked to exist before Finder is asked, so a
        crafted body gets a refusal rather than Finder bouncing silently.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, json.dumps({"error": "malformed request"}),
                              "application/json")
        wanted = os.path.abspath(os.path.expanduser(str(body.get("path") or "")))
        if not wanted or not os.path.lexists(wanted):
            return self._send(404, json.dumps({"ok": False,
                                               "error": "no longer there"}),
                              "application/json")
        try:
            subprocess.run(["open", "-R", wanted], capture_output=True,
                           timeout=15)
        except (OSError, subprocess.TimeoutExpired) as error:
            return self._send(500, json.dumps({"ok": False,
                                               "error": str(error)}),
                              "application/json")
        return self._send(200, json.dumps({"ok": True}), "application/json")

    def _cleanup(self):
        """Move everything the cleanup plan lists to the Trash.

        The plan is recomputed here rather than trusted from the request, so
        this endpoint can only ever remove what /api/cleanup would have shown
        -- a stale or crafted body cannot widen it into a second, unlisted
        delete. And it is intersected with the paths the page actually
        confirmed, so something that appeared between the confirmation being
        read and this arriving is not swept unread. The refusals in
        space.trash still apply on top.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        confirmed = body.get("paths")
        plan = space.cleanup_plan()
        paths = [item["path"] for group in plan["groups"]
                 for item in group["items"]]
        if isinstance(confirmed, list) and confirmed:
            wanted = {str(p) for p in confirmed}
            paths = [p for p in paths if p in wanted]
        if not paths:
            return self._send(200, json.dumps(
                {"ok": True, "results": [], "moved": 0, "bytes": 0}),
                "application/json")
        results = space.trash(paths)
        moved = [r for r in results if r["ok"]]
        by_path = {item["path"]: item["bytes"] for group in plan["groups"]
                   for item in group["items"]}
        freed = sum(by_path.get(r["path"], 0) for r in moved)
        return self._send(200, json.dumps(
            {"ok": True, "results": results, "moved": len(moved),
             "bytes": freed}), "application/json")

    def _netblock(self):
        """Stop an application using the network, or let it go again.

        By suspending it, which is the only per-application off switch a
        program without Apple's network-extension entitlement can offer: a
        process that is not running cannot send or receive. Blunter than a
        firewall rule -- the whole application freezes, not just its traffic
        -- and exactly reversible, which is what makes it honest to offer at
        all rather than a checkbox that does nothing.

        The pids come from a fresh reading rather than from the request, so a
        crafted body cannot aim SIGSTOP at something that was never on the
        page, and procs.signal_pid still refuses anything this user does not
        own.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, json.dumps({"error": "malformed request"}),
                              "application/json")
        name = str(body.get("app") or "")
        stop = bool(body.get("stop"))
        if not name:
            return self._send(400, json.dumps({"error": "no application named"}),
                              "application/json")
        wanted, is_system = [], False
        for entry in live.network_traffic()["apps"]:
            if entry["app"] == name:
                wanted = entry["pids"]
                is_system = entry["is_system"]
        if not wanted:
            return self._send(404, json.dumps(
                {"error": "%s holds no connection now" % name}),
                "application/json")
        if is_system and stop:
            # Freezing a piece of macOS is how a Mac stops answering its own
            # network, its keychain, or its window server.
            return self._send(400, json.dumps(
                {"error": "%s is part of macOS; suspending it would take the "
                          "system down with it" % name}), "application/json")
        # Never this program. Applications are grouped by the bundle their
        # executable lives in, and a Procwatch running on somebody's Python
        # is grouped with everything else using that Python -- so "turn off
        # Xcode" reached the server answering the request, which suspended
        # itself mid-reply and could not be resumed from a page it was no
        # longer serving. Verified the hard way.
        ours = {os.getpid(), os.getppid()}
        if stop and ours & set(wanted):
            return self._send(400, json.dumps(
                {"error": "%s is grouped with Procwatch itself, and "
                          "suspending it would freeze the window you are "
                          "reading" % name}), "application/json")
        results = procs.signal_group(wanted, "STOP" if stop else "CONT")
        done = [r for r in results if r["ok"]]
        return self._send(200 if done else 400, json.dumps(
            {"ok": bool(done), "stopped": stop, "app": name,
             "count": len(done), "results": results,
             "error": "" if done else (results[0].get("error") if results
                                       else "nothing to signal")}),
            "application/json")

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
            page = dashboard_html()
        except OSError:
            return self._send(404, "no dashboard installed", "text/plain")
        page = page.replace("__PROCWATCH_TOKEN__", _token_of(self.server))
        self._send(200, page.encode(), "text/html; charset=utf-8")


def serve(port, open_browser=True, idle_timeout=IDLE_TIMEOUT):
    if not os.path.exists(config.DB_PATH):
        print("no database yet; run `procwatch install` and wait a minute")
        return 1
    # The recorder notices new versions too, but a Mac running only the
    # dashboard still deserves the "Procwatch was updated" event.
    try:
        conn = db.connect(config.DB_PATH)
        try:
            updated = selfupdate.note_if_updated(conn)
            if updated:
                alerts.announce(conn, "Procwatch updated",
                                "Now running %s (was %s)"
                                % (updated["to"], updated["from"]),
                                target="events")
            # With no menu bar app to collect the queue -- and possibly no
            # recorder to sweep it -- whatever is waiting goes out the old
            # way now rather than never.
            if not alerts.bar_running():
                alerts.deliver_stale(conn, wait=0)
        finally:
            conn.close()
    except Exception:
        pass          # a missed note must not cost the dashboard
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
