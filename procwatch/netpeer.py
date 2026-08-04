"""Which servers this Mac talked to, and when -- kept, so the map can look back.

The live network panel has always known who the machine is talking to right
now, because a peer is readable while its socket is open. Nothing wrote that
down, so every past window on the map was empty: the recording knew how many
bytes an application moved at 14:20 but not where they went. Asking the map
for six hours ago returned an honest nothing.

This is the missing half. One row per peer per application per five-minute
bucket, kept for a week.

Three decisions worth stating, because each of them is a trade someone might
want made differently.

PASSIVE ONLY. Nothing here sends a packet. The peers recorded are the ones
that were already exchanging traffic with this machine; a monitoring tool that
starts probing third-party servers to decorate its own display is doing
something its user did not ask for and cannot see.

FIVE-MINUTE BUCKETS, SEVEN DAYS. At the sampler's own 30-second cadence this
table would outgrow the sample history it decorates -- roughly 200 MB a month
against 20 MB at five minutes. Five minutes is finer than the question ("what
was talking to my Mac this afternoon") and a tenth of the disk.

A CONNECTION SHORTER THAN ONE TICK IS NOT COUNTED. Its only sighting is its
first, and a first sighting has nothing to difference against, so it
contributes nothing. That undercounts brief requests -- a single DNS lookup, a
one-shot API call -- and the alternative is worse: crediting a socket's entire
lifetime to whichever bucket happened to notice it.

BYTES ARE DIFFERENCED, NEVER READ RAW. nettop's per-connection counters are
cumulative for the life of the socket, so the number in a bucket has to be the
delta against that same socket's previous sighting. Summing the raw counters
would report a long-lived connection's entire lifetime in every bucket it
appears in, which turns one steady download into a graph that climbs forever.
"""
import time

from . import geoip

DDL = """
CREATE TABLE IF NOT EXISTS net_peer (
  ts        INTEGER NOT NULL,
  ip        TEXT    NOT NULL,
  app       TEXT    NOT NULL,
  bytes_in  INTEGER NOT NULL,
  bytes_out INTEGER NOT NULL,
  PRIMARY KEY (ts, ip, app)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS net_peer_ts ON net_peer (ts);

CREATE TABLE IF NOT EXISTS net_peer_state (
  conn       TEXT PRIMARY KEY,
  bytes_in   INTEGER NOT NULL,
  bytes_out  INTEGER NOT NULL,
  updated_ts INTEGER NOT NULL
);
"""

BUCKET = 300
KEEP = 7 * 86400
# A socket unseen for this long is closed. Its counters are dropped so a later
# connection reusing the same local port is differenced from zero rather than
# from a stranger's total, which would otherwise show as a negative delta and
# be discarded -- losing that connection's first bucket.
STATE_TTL = 3600


def bucket_of(ts):
    return int(ts) - (int(ts) % BUCKET)


def _key(app, conn):
    """Identity of one socket, stable across ticks.

    The local port is part of it: two connections from the same application to
    the same server are separate sockets with separate counters, and folding
    them together would difference one against the other.
    """
    return "%s|%s|%s|%s" % (app, conn.get("proto", ""),
                            conn.get("local", ""), conn.get("remote", ""))


def _delta(previous, current):
    """Cumulative counters differenced, discarding a counter that went
    backwards -- which means the socket is not the one we measured before."""
    if previous is None or current < previous:
        return 0
    return current - previous


def record(conn, rows, now=None):
    """Fold one nettop pass into the current bucket.

    `rows` is netstat.traffic() output. Returns the number of peer rows
    touched, which is what the tests assert on.
    """
    now = int(time.time()) if now is None else int(now)
    slot = bucket_of(now)

    previous = {r[0]: (r[1], r[2]) for r in
                conn.execute("SELECT conn, bytes_in, bytes_out "
                             "FROM net_peer_state")}
    totals = {}
    state = []
    for row in rows:
        app = row.get("name") or ""
        for socket in row.get("conns", []):
            host = socket.get("host") or ""
            if not host or host.startswith("*"):
                continue
            key = _key(app, socket)
            got_in = int(socket.get("bytes_in") or 0)
            got_out = int(socket.get("bytes_out") or 0)
            was = previous.get(key)
            gained_in = _delta(was[0] if was else None, got_in)
            gained_out = _delta(was[1] if was else None, got_out)
            state.append((key, got_in, got_out, now))
            if not gained_in and not gained_out:
                continue
            cell = totals.setdefault((slot, host, app), [0, 0])
            cell[0] += gained_in
            cell[1] += gained_out

    with conn:
        if state:
            conn.executemany(
                "INSERT INTO net_peer_state (conn, bytes_in, bytes_out, updated_ts) "
                "VALUES (?,?,?,?) ON CONFLICT(conn) DO UPDATE SET "
                "bytes_in=excluded.bytes_in, bytes_out=excluded.bytes_out, "
                "updated_ts=excluded.updated_ts", state)
        if totals:
            # Accumulating rather than replacing: a bucket is written on every
            # tick inside its five minutes, and each one adds its own delta.
            conn.executemany(
                "INSERT INTO net_peer (ts, ip, app, bytes_in, bytes_out) "
                "VALUES (?,?,?,?,?) ON CONFLICT(ts, ip, app) DO UPDATE SET "
                "bytes_in = bytes_in + excluded.bytes_in, "
                "bytes_out = bytes_out + excluded.bytes_out",
                [(k[0], k[1], k[2], v[0], v[1]) for k, v in totals.items()])
    return len(totals)


def prune(conn, now=None):
    """Drop history past the retention window, and counters for dead sockets."""
    now = int(time.time()) if now is None else int(now)
    with conn:
        conn.execute("DELETE FROM net_peer WHERE ts < ?", (now - KEEP,))
        conn.execute("DELETE FROM net_peer_state WHERE updated_ts < ?",
                     (now - STATE_TTL,))


def peers(conn, start, end, limit=400):
    """Every peer seen in a window, with where it is, busiest first.

    geoip.where does not block: an address it has not seen is queued and
    answered on a later call, so a window full of new peers returns at once
    with no coordinates and fills in as the map is left open. Passing
    allow_lookup=False here instead would mean an address is only ever placed
    if something else happened to ask about it first -- which for history is
    almost never, since the live panel only ever sees current connections.
    """
    rows = conn.execute(
        "SELECT ip, app, SUM(bytes_in), SUM(bytes_out) FROM net_peer "
        "WHERE ts >= ? AND ts < ? GROUP BY ip, app "
        "ORDER BY SUM(bytes_in) + SUM(bytes_out) DESC LIMIT ?",
        (int(start), int(end), limit)).fetchall()
    # Whether an application is part of macOS, so the history obeys the same
    # "hide system processes" switch the live panel does. Read from the
    # identities the sampler already interned rather than decided again here;
    # an identity is macOS only if every process recorded under it was.
    system = {}
    for name, flag in conn.execute(
            "SELECT COALESCE(NULLIF(app, ''), exe), MIN(is_system) "
            "FROM proc GROUP BY 1"):
        system[name] = bool(flag)
    out = []
    for ip, app, got_in, got_out in rows:
        place = geoip.where(ip) or {}
        out.append({"ip": ip, "app": app,
                    "is_system": system.get(app, False),
                    "bytes_in": got_in or 0, "bytes_out": got_out or 0,
                    "private": geoip.is_private(ip),
                    "lat": place.get("lat"), "lon": place.get("lon"),
                    "city": place.get("city", ""),
                    "country": place.get("country", ""),
                    "org": place.get("org", "")})
    return out


def timeline(conn, start, end, slots=240):
    """Total bytes per slot across a window, for the scrubber's own track.

    The scrubber needs to show where the activity is before you drag to it --
    a timeline with no shape is a slider with nothing to aim at.
    """
    start, end = int(start), int(end)
    span = max(1, end - start)
    width = max(BUCKET, span // max(1, slots))
    rows = conn.execute(
        "SELECT ts, SUM(bytes_in + bytes_out), COUNT(DISTINCT ip) "
        "FROM net_peer WHERE ts >= ? AND ts < ? GROUP BY ts ORDER BY ts",
        (start, end)).fetchall()
    slotted = {}
    for ts, total, distinct in rows:
        key = start + ((ts - start) // width) * width
        cell = slotted.setdefault(key, [0, 0])
        cell[0] += total or 0
        cell[1] = max(cell[1], distinct or 0)
    return {"start": start, "end": end, "width": width,
            "points": [{"ts": ts, "bytes": v[0], "peers": v[1]}
                       for ts, v in sorted(slotted.items())]}


def span(conn):
    """The oldest and newest history held, so the scrubber knows its limits."""
    row = conn.execute("SELECT MIN(ts), MAX(ts) FROM net_peer").fetchone()
    if not row or row[0] is None:
        return None
    return {"first": row[0], "last": row[1] + BUCKET}
