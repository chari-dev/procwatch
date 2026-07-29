"""Other Macs running the recorder.

Each machine still records for itself, into its own database; nothing is
centralised. What this adds is that one dashboard can ask another machine for
its answers.

The machine being watched runs `procwatch share`, which opens a read-only
listener and prints a three-word key. The machine doing the watching stores
the address and the key. That is the whole arrangement: no accounts, no
certificates, no shell access.

Reads only, and not because a flag says so -- the listener on the other side
has no route to anything that can end a process or copy a database. So the
worst case, if a key leaks, is that someone learns which applications you run.
"""
import json
import os
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

from . import config, share

DDL = """
CREATE TABLE IF NOT EXISTS peer (
  name      TEXT PRIMARY KEY,
  host      TEXT NOT NULL,
  key       TEXT NOT NULL DEFAULT '',
  added_ts  INTEGER NOT NULL
);
"""

# Long enough for a year-wide query over wi-fi, short enough that a machine
# that is asleep does not hold up the page.
TIMEOUT = 30


def _db():
    from . import db
    conn = db.connect(config.DB_PATH)
    with conn:
        conn.executescript(DDL)
        # Databases from the SSH version have a `program` column and no `key`.
        columns = {r[1] for r in conn.execute("PRAGMA table_info(peer)")}
        if "key" not in columns:
            conn.execute("ALTER TABLE peer ADD COLUMN key TEXT NOT NULL DEFAULT ''")
    return conn


def normalise(host):
    """Accept what people actually type.

    "192.168.1.42", "192.168.1.42:8791", "http://192.168.1.42:8791" and
    "laptop.local" all mean the same thing, and being told off for the wrong
    one is a poor first experience of a feature whose whole point is that it
    is simple.
    """
    host = (host or "").strip().rstrip("/")
    if not host:
        raise ValueError("a device needs an address")
    if "://" not in host:
        host = "http://" + host
    if host.count(":") < 2:                       # no port given
        host = "%s:%d" % (host, share.DEFAULT_PORT)
    return host


def add(name, host, key=""):
    if not name:
        raise ValueError("a device needs a name")
    if name.lower() == "this mac":
        raise ValueError("that name is reserved for the local machine")
    host = normalise(host)
    conn = _db()
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO peer (name, host, key, added_ts) "
                "VALUES (?,?,?,?)", (name, host, key.strip(), int(time.time())))
    finally:
        conn.close()
    return host


def remove(name):
    conn = _db()
    try:
        with conn:
            cur = conn.execute("DELETE FROM peer WHERE name = ?", (name,))
        return cur.rowcount > 0
    finally:
        conn.close()


def listing(with_keys=False):
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT name, host, key FROM peer ORDER BY name").fetchall()
    finally:
        conn.close()
    # The key never goes to the browser: the page has no use for it, and a
    # dashboard left open on a shared screen should not display the keys to
    # every other machine you own.
    return [{"name": r[0], "host": r[1],
             **({"key": r[2]} if with_keys else {})} for r in rows]


def _one(name):
    for peer in listing(with_keys=True):
        if peer["name"] == name:
            return peer
    raise KeyError("no device called %r" % name)


def fetch(name, path, params):
    """Ask a device for one API path."""
    peer = _one(name)
    flat = []
    for key_name, values in params.items():
        for value in values:
            flat.append((key_name, value))
    url = peer["host"] + path + ("?" + urlencode(flat) if flat else "")
    request = urllib.request.Request(url, headers={share.HEADER: peer["key"]})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = json.loads(error.read().decode()).get("error", "")
        except Exception:
            pass
        if error.code == 401:
            raise RuntimeError("%s refused the key" % name)
        if error.code == 429:
            raise RuntimeError("%s is refusing keys for a moment" % name)
        raise RuntimeError(detail or "%s answered %d" % (name, error.code))
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", error)
        # EHOSTUNREACH from inside an app bundle is almost never a routing
        # problem: macOS refuses an app's local network access until it is
        # allowed, and reports the refusal as "no route to host". The same
        # request from a terminal succeeds, which is exactly what makes it
        # baffling to diagnose from the message alone.
        if getattr(reason, "errno", None) == 65 and _inside_app_bundle():
            raise RuntimeError(
                "macOS is blocking Procwatch from your local network, so it "
                "cannot reach %s. Allow it in System Settings \u203a Privacy & "
                "Security \u203a Local Network, then reopen Procwatch." % name)
        raise RuntimeError(
            "cannot reach %s. Is it awake, and is `procwatch share` running "
            "there? (%s)" % (name, reason))
    except ValueError:
        raise RuntimeError("%s did not return data" % name)


def _inside_app_bundle():
    """Whether this process was started by the menu bar app.

    Only the app is subject to the local network prompt; the same code run
    from a terminal inherits the terminal's permission and works.
    """
    import sys
    entry = (sys.argv[0] if sys.argv else "") or ""
    return ".app/Contents/" in os.path.abspath(entry)


def check(name):
    """Whether a device answers, and what it is."""
    try:
        info = fetch(name, "/api/info", {})
    except (RuntimeError, KeyError, OSError) as error:
        return {"name": name, "ok": False, "error": str(error)[:200]}
    # Against that machine's clock, not ours: two Macs can be hours apart in
    # absolute time, which would report a healthy recorder as long stale.
    age = None
    if info.get("last_tick"):
        reference = int(info.get("now") or time.time())
        age = reference - int(info["last_tick"])
    return {"name": name, "ok": True, "hostname": info.get("hostname", ""),
            "chip": info.get("chip", ""), "cores": info.get("cores"),
            "last_tick_age": age}
