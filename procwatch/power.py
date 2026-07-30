"""Why the Mac woke, what kept it awake, and where the battery went.

Three questions nobody can answer the next morning, and all three are
answerable from data macOS already keeps -- none of it needing a password.

The design point is cost. `pmset -g log` holds the full history, and on this
machine that is 53 MB and ten seconds of CPU to read, which is an absurd thing
for a tool that watches CPU to do on a schedule. But `pmset -g assertions`
costs 13 milliseconds and already reports how long each hold has lasted. So
holds are observed continuously and cheaply, and the expensive log is read
rarely and only for the thing observation cannot see: what happened while we
were not running, because the machine was asleep.
"""
import re
import subprocess
import time
from datetime import datetime

from . import config

DDL = """
CREATE TABLE IF NOT EXISTS power_hold (
  aid       TEXT PRIMARY KEY,
  pid       INTEGER NOT NULL,
  process   TEXT NOT NULL,
  kind      TEXT NOT NULL,
  name      TEXT NOT NULL DEFAULT '',
  first_ts  INTEGER NOT NULL,
  last_ts   INTEGER NOT NULL,
  seconds   INTEGER NOT NULL,
  open      INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS power_hold_last ON power_hold (last_ts);

CREATE TABLE IF NOT EXISTS power_event (
  ts      INTEGER PRIMARY KEY,
  kind    TEXT NOT NULL,
  reason  TEXT NOT NULL DEFAULT '',
  charge  INTEGER NOT NULL DEFAULT -1,
  on_ac   INTEGER NOT NULL DEFAULT -1
);

CREATE TABLE IF NOT EXISTS power_state (
  key   TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);
"""

# Assertions that stop the machine sleeping. The distinction matters: a
# display assertion keeps the screen on and is usually the person sitting
# there, while these keep the whole machine running in a bag.
SYSTEM_HOLDS = (
    "PreventUserIdleSystemSleep",
    "PreventSystemSleep",
    "NoIdleSleepAssertion",
    "NetworkClientActive",
)

# Held by the system on your behalf while you are using it. Reporting these as
# something that "kept your Mac awake" would be telling you that you did.
NOT_YOUR_PROBLEM = ("UserIsActive", "InternalPreventDisplaySleep")

# The full log is read at most this often. Six hours rather than daily so a
# machine that slept overnight explains itself by morning.
IMPORT_EVERY = 6 * 3600

_HOLD = re.compile(
    r"pid (\d+)\(([^)]*)\):\s*\[(0x[0-9a-fA-F]+)\]\s*([\d:]+)\s+(\w+)"
    r"(?:\s+named:\s*\"(.*?)\")?")

_LOG_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [-+]\d{4})\s+(\S+(?: \S+)?)\s+(.*)$")

_CHARGE = re.compile(r"Using (AC|BATT|Batt)\s*\(?\s*Charge:\s*(\d+)", re.I)
_SLEEP_REASON = re.compile(r"due to '([^']+)'")


# Driver strings to plain language.
#
# The log answers "why did it wake" with things like
# "smc.sysState.Wake(0x70070000) lid MTP.DOCK.CHANNELS.AP0.IRQ
# RTP.multi-touch/UserActivity Assertion". That is the answer, but nobody can
# read it. Ordered: the first match wins, so a lid opening beats the network
# activity that also happened.
WAKE_MEANINGS = (
    ("lid", "you opened the lid"),
    ("UserActivity", "you used it"),
    ("multi-touch", "you touched the trackpad"),
    ("HID", "a key or the trackpad"),
    ("PWRB", "the power button"),
    ("Maintenance", "scheduled maintenance"),
    ("TCPKeepAlive", "keeping a network connection alive"),
    ("SleepService", "a background task"),
    ("dasd", "a background task"),
    ("bluetooth", "a Bluetooth device"),
    ("wifibt", "Wi-Fi or Bluetooth"),
    ("wlan", "Wi-Fi"),
    ("BATTERY", "the battery"),
    ("MAGICWAKE", "something on the network"),
    ("USB", "a USB device"),
    ("RTC", "a timer"),
    ("EC.", "the hardware controller"),
    ("smc", "the hardware controller"),
)

SLEEP_MEANINGS = (
    ("Maintenance Sleep", "a maintenance nap"),
    ("Idle Sleep", "being left alone"),
    ("Software Sleep", "being told to"),
    ("Clamshell Sleep", "the lid closing"),
    ("Low Power", "a low battery"),
)


def describe_wake(reason):
    """A sentence a person can read, from a driver string."""
    for token, meaning in WAKE_MEANINGS:
        if token.lower() in (reason or "").lower():
            return meaning
    return "an unknown cause"


def describe_sleep(reason):
    for token, meaning in SLEEP_MEANINGS:
        if token.lower() in (reason or "").lower():
            return meaning
    return (reason or "sleep").strip()


def init(conn):
    with conn:
        conn.executescript(DDL)


def _seconds(text):
    """"115:19:14" -> seconds. Hours are not wrapped at 24."""
    parts = [int(p) for p in text.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _stamp(text):
    """A pmset timestamp to epoch seconds.

    The offset is in the line, so it is parsed rather than guessed -- which
    also sidesteps the daylight-saving trap that comes from feeding a local
    time to a function that has to infer the offset.
    """
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z").timestamp())


def read_holds():
    """Everything currently holding this Mac awake, with how long so far."""
    try:
        out = subprocess.run(["pmset", "-g", "assertions"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = []
    for line in out.stdout.splitlines():
        match = _HOLD.search(line)
        if not match:
            continue
        pid, process, aid, duration, kind, name = match.groups()
        if kind in NOT_YOUR_PROBLEM:
            continue
        found.append({"aid": aid, "pid": int(pid), "process": process,
                      "kind": kind, "name": name or "",
                      "seconds": _seconds(duration)})
    return found


def tick(conn, now=None):
    """Record what is holding the machine awake, and close what stopped.

    An assertion's id is unique to that hold, so seeing it again is the same
    hold continuing and not seeing it means it ended. pmset's own duration is
    stored rather than one derived from when we happened to look, so a hold is
    reported accurately even if the recorder missed a few ticks.
    """
    init(conn)
    now = int(time.time()) if now is None else now
    holds = read_holds()
    seen = set()
    with conn:
        for hold in holds:
            seen.add(hold["aid"])
            began = now - hold["seconds"]
            conn.execute(
                "INSERT INTO power_hold (aid, pid, process, kind, name, "
                "first_ts, last_ts, seconds, open) VALUES (?,?,?,?,?,?,?,?,1) "
                "ON CONFLICT(aid) DO UPDATE SET last_ts = excluded.last_ts, "
                "seconds = excluded.seconds, open = 1",
                (hold["aid"], hold["pid"], hold["process"], hold["kind"],
                 hold["name"], began, now, hold["seconds"]))
        if seen:
            placeholders = ",".join("?" * len(seen))
            conn.execute(
                "UPDATE power_hold SET open = 0 WHERE open = 1 "
                "AND aid NOT IN (%s)" % placeholders, tuple(seen))
        else:
            conn.execute("UPDATE power_hold SET open = 0 WHERE open = 1")
    return len(holds)


def due(conn, now=None, every=IMPORT_EVERY):
    init(conn)
    now = int(time.time()) if now is None else now
    row = conn.execute(
        "SELECT value FROM power_state WHERE key = 'imported_ts'").fetchone()
    return row is None or now - row[0] >= every


def import_log(conn, now=None, text=None):
    """Read sleeps and wakes out of the power log.

    Only the events, not the assertions: the log's 212,000 assertion lines are
    already covered by observation, and they are what makes reading it
    expensive. Rows are keyed by timestamp so re-importing an overlapping
    window costs nothing and changes nothing.
    """
    init(conn)
    now = int(time.time()) if now is None else now
    if text is None:
        try:
            out = subprocess.run(["pmset", "-g", "log"], capture_output=True,
                                 text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired):
            return 0
        text = out.stdout

    rows = []
    for line in text.splitlines():
        match = _LOG_LINE.match(line)
        if not match:
            continue
        when, label, rest = match.groups()
        label = label.strip()
        if label == "Sleep":
            kind = "sleep"
        elif label == "Wake":
            kind = "wake"
        elif label == "DarkWake":
            kind = "darkwake"
        else:
            continue
        try:
            ts = _stamp(when)
        except ValueError:
            continue

        charge, on_ac = -1, -1
        found = _CHARGE.search(rest)
        if found:
            on_ac = 1 if found.group(1).upper() == "AC" else 0
            charge = int(found.group(2))

        if kind == "sleep":
            reason = _SLEEP_REASON.search(rest)
            detail = reason.group(1) if reason else "sleep"
        else:
            # "Wake from Deep Idle [CDNVA] : due to smc... lid ... Assertion"
            detail = rest.split(":", 1)[-1] if ":" in rest else rest
            detail = re.sub(r"Using (AC|BATT|Batt).*$", "", detail).strip()
            detail = re.sub(r"\s+", " ", detail)[:180] or kind
        rows.append((ts, kind, detail, charge, on_ac))

    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO power_event (ts, kind, reason, charge, on_ac) "
            "VALUES (?,?,?,?,?)", rows)
        conn.execute("INSERT OR REPLACE INTO power_state (key, value) "
                     "VALUES ('imported_ts', ?)", (now,))
    return len(rows)


def holding_now(conn):
    """What is keeping this Mac awake at this moment, longest first."""
    init(conn)
    rows = conn.execute(
        "SELECT process, kind, name, seconds, pid FROM power_hold "
        "WHERE open = 1 ORDER BY seconds DESC").fetchall()
    return [{"process": r[0], "kind": r[1], "name": r[2], "seconds": r[3],
             "pid": r[4], "prevents_sleep": r[1] in SYSTEM_HOLDS}
            for r in rows]


def kept_awake(conn, start, end, limit=12):
    """Who held the machine awake during a window, ranked by time held.

    A hold is counted for the part of it that falls inside the window, so a
    process that has held an assertion for five days does not report five days
    against last night.
    """
    init(conn)
    rows = conn.execute(
        "SELECT process, kind, name, first_ts, last_ts, seconds FROM power_hold "
        "WHERE last_ts > ? AND first_ts < ?", (start, end)).fetchall()
    totals = {}
    for process, kind, name, first_ts, last_ts, seconds in rows:
        if kind not in SYSTEM_HOLDS:
            continue
        overlap = min(last_ts, end) - max(first_ts, start)
        if overlap <= 0:
            continue
        entry = totals.setdefault(process, {"process": process, "seconds": 0,
                                            "kinds": set(), "names": set()})
        entry["seconds"] += overlap
        entry["kinds"].add(kind)
        if name:
            entry["names"].add(name)
    out = sorted(totals.values(), key=lambda e: -e["seconds"])[:limit]
    for entry in out:
        entry["kinds"] = sorted(entry["kinds"])
        entry["names"] = sorted(entry["names"])[:3]
    return out


def nights(conn, start, end):
    """Sleeps and wakes in a window, paired, with what the battery did.

    A wake immediately following a sleep closes it. The battery figure is the
    difference between the charge recorded at each end, which is the number
    people actually want: how much it cost to leave the lid shut.
    """
    init(conn)
    rows = conn.execute(
        "SELECT ts, kind, reason, charge, on_ac FROM power_event "
        "WHERE ts >= ? AND ts <= ? ORDER BY ts", (start, end)).fetchall()
    out, pending = [], None
    for ts, kind, reason, charge, on_ac in rows:
        if kind == "sleep":
            pending = {"slept_ts": ts, "reason": reason, "charge_at_sleep": charge,
                       "on_ac": on_ac}
            continue
        if pending is None:
            out.append({"slept_ts": None, "woke_ts": ts, "kind": kind,
                        "wake_reason": reason, "woke_because": describe_wake(reason),
                        "charge_at_wake": charge,
                        "asleep_seconds": None, "charge_lost": None,
                        "on_ac": on_ac})
            continue
        used = None
        if pending["charge_at_sleep"] >= 0 and charge >= 0:
            used = pending["charge_at_sleep"] - charge
        out.append({"slept_ts": pending["slept_ts"], "woke_ts": ts, "kind": kind,
                    "sleep_reason": pending["reason"], "wake_reason": reason,
                    "woke_because": describe_wake(reason),
                    "slept_because": describe_sleep(pending["reason"]),
                    "charge_at_sleep": pending["charge_at_sleep"],
                    "charge_at_wake": charge,
                    "asleep_seconds": ts - pending["slept_ts"],
                    "charge_lost": used,
                    "on_ac": pending["on_ac"]})
        pending = None
    return out


def overnight_drain(conn, start, end):
    """The headline: how much charge the lid being shut cost, and how often it
    woke while shut.

    Only stretches on battery count. Losing charge on mains is a different
    fault and reporting it here would bury the one people mean.
    """
    spans = [n for n in nights(conn, start, end)
             if n.get("asleep_seconds") and n.get("charge_lost") is not None
             and n.get("on_ac") == 0]
    if not spans:
        return None
    lost = sum(max(0, n["charge_lost"]) for n in spans)
    asleep = sum(n["asleep_seconds"] for n in spans)
    dark = sum(1 for n in spans if n.get("kind") == "darkwake")
    return {"charge_lost": lost, "asleep_seconds": asleep,
            "wakes": len(spans), "dark_wakes": dark,
            "per_hour": (lost / (asleep / 3600.0)) if asleep > 60 else None}


def prune(conn, now=None):
    """Forget holds and events older than the longest tier keeps samples."""
    init(conn)
    now = int(time.time()) if now is None else now
    keep = max(t.keep for t in config.TIERS if t.keep) if any(
        t.keep for t in config.TIERS) else 365 * 86400
    cutoff = now - keep
    with conn:
        conn.execute("DELETE FROM power_hold WHERE last_ts < ? AND open = 0",
                     (cutoff,))
        conn.execute("DELETE FROM power_event WHERE ts < ?", (cutoff,))
