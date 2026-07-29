"""Other Macs running the recorder, reached over SSH.

The design note said one laptop, local storage, local viewing. This relaxes
the viewing half only: each machine still records for itself, into its own
database, and nothing is centralised. What changes is that one dashboard can
ask another machine for its answers.

Over SSH, deliberately, rather than by opening the recorder's HTTP server to
the network. That server has a kill endpoint on it. Binding it to anything but
the loopback would put "terminate a process on this Mac" one unauthenticated
request away from everyone on the coffee shop wi-fi, and defending that
properly means inventing an authentication scheme. SSH already solved this,
the keys are already there, and nothing has to listen on a port that was not
listening before.

A peer answers by running its own copy of the program with `fetch`, so the
reply comes from exactly the code that would have served it locally.
"""
import json
import os
import shlex
import subprocess
import time
from urllib.parse import urlencode

from . import config

DDL = """
CREATE TABLE IF NOT EXISTS peer (
  name      TEXT PRIMARY KEY,
  host      TEXT NOT NULL,
  program   TEXT NOT NULL DEFAULT '',
  added_ts  INTEGER NOT NULL
);
"""

# Where the installer puts the program on any machine. A peer that keeps it
# elsewhere can say so when it is added.
DEFAULT_PROGRAM = "$HOME/Library/Application Support/procwatch/procwatch.py"

# Long enough for a year-wide query on a slow link, short enough that a
# machine which is asleep does not hang the dashboard.
TIMEOUT = 45

# Asking a sleeping laptop should fail quickly rather than block the page.
CONNECT_TIMEOUT = 8


def _db():
    from . import db
    conn = db.connect(config.DB_PATH)
    with conn:
        conn.executescript(DDL)
    return conn


def add(name, host, program=""):
    if not name or not host:
        raise ValueError("a peer needs a name and an ssh host")
    if name.lower() == "this mac":
        raise ValueError("that name is reserved for the local machine")
    conn = _db()
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO peer (name, host, program, added_ts) "
                "VALUES (?,?,?,?)", (name, host, program, int(time.time())))
    finally:
        conn.close()


def remove(name):
    conn = _db()
    try:
        with conn:
            cur = conn.execute("DELETE FROM peer WHERE name = ?", (name,))
        return cur.rowcount > 0
    finally:
        conn.close()


def listing():
    conn = _db()
    try:
        return [{"name": r[0], "host": r[1], "program": r[2]}
                for r in conn.execute(
                    "SELECT name, host, program FROM peer ORDER BY name")]
    finally:
        conn.close()


def _one(name):
    for peer in listing():
        if peer["name"] == name:
            return peer
    raise KeyError("no peer called %r" % name)


def fetch(name, path, params):
    """Ask a peer for one API path. Returns whatever that machine returns.

    The path and query are rebuilt here rather than passed through as a string
    from the browser, so what reaches the remote command line is assembled
    from values this process chose.
    """
    peer = _one(name)
    program = peer["program"] or DEFAULT_PROGRAM
    flat = []
    for key, values in params.items():
        for value in values:
            flat.append((key, value))
    query = urlencode(flat)
    # shlex.quote on every piece: a peer's stored program path and the query
    # both end up inside a shell command on the far side.
    remote = "python3 %s fetch %s %s" % (
        shlex.quote(program) if "$HOME" not in program else '"%s"' % program,
        shlex.quote(path), shlex.quote(query))
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes",
         "-o", "ConnectTimeout=%d" % CONNECT_TIMEOUT,
         "-o", "StrictHostKeyChecking=accept-new",
         peer["host"], remote],
        capture_output=True, text=True, timeout=TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[:300]
                           or "ssh to %s failed" % peer["host"])
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise RuntimeError(
            "%s did not return data. Is procwatch installed there? (%s)"
            % (name, (result.stdout or "").strip()[:120]))


def check(name):
    """Whether a peer answers, and what it is. Used by the device switcher."""
    try:
        info = fetch(name, "/api/info", {})
    except (RuntimeError, KeyError, OSError, subprocess.TimeoutExpired) as error:
        return {"name": name, "ok": False, "error": str(error)[:200]}
    # Against the peer's clock, not ours. These two machines are ten hours
    # apart in absolute time, which would otherwise report a healthy recorder
    # as ten hours stale.
    age = None
    if info.get("last_tick"):
        reference = int(info.get("now") or time.time())
        age = reference - int(info["last_tick"])
    return {"name": name, "ok": True, "hostname": info.get("hostname", ""),
            "chip": info.get("chip", ""), "cores": info.get("cores"),
            "last_tick_age": age}
