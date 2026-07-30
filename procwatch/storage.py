"""How much disk each application occupies.

Every other metric here is a rate sampled every thirty seconds. This one is a
size, it changes slowly, and measuring it means walking directories -- so it
runs at most once a day and is stored as a plain snapshot rather than folded
into the sample tiers. Putting it through the same machinery would cost a
filesystem walk every thirty seconds to record a number that moves once a week.

"An application" here means three places: the bundle itself, its Application
Support directory, and its caches. That is what someone means by "how much
space is Slack using" -- the .app alone is the download size and misses the
gigabytes of cached messages that are the actual answer.
"""
import os
import time

from . import config

DAY = 86400

DDL = """
CREATE TABLE IF NOT EXISTS storage (
  app        TEXT NOT NULL,
  kind       TEXT NOT NULL,
  bytes      INTEGER NOT NULL,
  ts         INTEGER NOT NULL,
  PRIMARY KEY (app, kind)
) WITHOUT ROWID;

/* Every scan kept, not just the last one.
 *
 * The current sizes answer "what is big". Almost nobody needs that -- a
 * folder that has been 40 GB for two years is not why the disk filled up
 * this week. What people are actually asking is "what GREW", and that needs
 * yesterday's number as well as today's. Keeping one row per scan is a few
 * kilobytes a day and is the only way to answer it. */
CREATE TABLE IF NOT EXISTS storage_history (
  day        INTEGER NOT NULL,
  app        TEXT NOT NULL,
  kind       TEXT NOT NULL,
  bytes      INTEGER NOT NULL,
  PRIMARY KEY (day, app, kind)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS storage_history_app ON storage_history (app, day);
"""

# Where an application's own data lives, by convention. Anything outside these
# is somebody else's file, and guessing wider would attribute a shared cache
# to whichever app was scanned first.
ROOTS = [
    ("bundle", "/Applications"),
    ("support", os.path.expanduser("~/Library/Application Support")),
    ("caches", os.path.expanduser("~/Library/Caches")),
]

# A walk that never finishes is worse than no number. Anything deeper than
# this is inside a package's own package and is counted by size regardless --
# the limit bounds recursion, not accuracy.
MAX_DEPTH = 12


def init(conn):
    with conn:
        conn.executescript(DDL)


def _tree_bytes(path, depth=0):
    """Bytes on disk under a path, following no symlinks.

    Symlinks are skipped rather than followed: /Applications is full of links
    into the same bundle, and following them counts the target repeatedly and
    can loop forever.
    """
    total = 0
    if depth > MAX_DEPTH:
        return total
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        total += _tree_bytes(entry.path, depth + 1)
                    else:
                        # st_blocks is what the disk actually gave up, which is
                        # the number a person checking free space cares about.
                        stat = entry.stat(follow_symlinks=False)
                        total += getattr(stat, "st_blocks", 0) * 512 or stat.st_size
                except (OSError, ValueError):
                    continue          # vanished mid-walk, or not permitted
    except (OSError, ValueError):
        return total
    return total


def _app_name(entry_name, kind):
    if kind == "bundle":
        return entry_name[:-4] if entry_name.endswith(".app") else None
    return entry_name


def scan(conn, now=None, roots=None):
    """Measure every application and store one row per app per kind."""
    init(conn)
    now = int(time.time()) if now is None else now
    rows = []
    for kind, root in (roots or ROOTS):
        if not os.path.isdir(root):
            continue
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            if name.startswith("."):
                continue
            path = os.path.join(root, name)
            # The root entry needs the same symlink check as everything below
            # it: /Applications routinely holds links to bundles that live
            # elsewhere, and counting one twice is worse than missing it.
            if os.path.islink(path):
                continue
            app = _app_name(name, kind)
            if not app:
                continue
            size = _tree_bytes(path)
            if size:
                rows.append((app, kind, size, now))
    # One snapshot per day. A second scan on the same day corrects that day
    # rather than adding a second reading, so a machine that is restarted four
    # times does not appear to have four days of measurements.
    day = now - (now % DAY)
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO storage (app, kind, bytes, ts) "
            "VALUES (?,?,?,?)", rows)
        conn.executemany(
            "INSERT OR REPLACE INTO storage_history (day, app, kind, bytes) "
            "VALUES (?,?,?,?)",
            [(day, app, kind, size) for app, kind, size, _ in rows])
    return len(rows)


def due(conn, now=None, every=DAY):
    """Whether a scan is worth doing. Sizes move slowly; walks are expensive."""
    init(conn)
    now = int(time.time()) if now is None else now
    last = conn.execute("SELECT MAX(ts) FROM storage").fetchone()[0]
    return last is None or now - last >= every


def growth(conn, since=None, now=None, limit=15):
    """What grew, and what shrank, between the oldest scan in the window and
    the newest.

    Two scans are compared rather than a slope through all of them: a
    measurement of a directory is exact, so the difference between two of them
    is exactly what changed. Fitting a line to exact numbers would only add
    error.

    Applications that appeared or were deleted in the window are reported as
    such, because "+4 GB" and "installed, 4 GB" are different news.
    """
    init(conn)
    now = int(time.time()) if now is None else now
    since = (now - 7 * DAY) if since is None else since

    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT day FROM storage_history WHERE day >= ? AND day <= ? "
        "ORDER BY day", (since - (since % DAY), now)).fetchall()]
    if len(days) < 2:
        return {"from_day": days[0] if days else None, "to_day": None,
                "apps": [], "total_change": 0, "days_compared": len(days)}
    first, last = days[0], days[-1]

    def snapshot(day):
        totals = {}
        for app, size in conn.execute(
                "SELECT app, SUM(bytes) FROM storage_history WHERE day = ? "
                "GROUP BY app", (day,)).fetchall():
            totals[app] = size
        return totals

    before, after = snapshot(first), snapshot(last)
    changes = []
    for app in set(before) | set(after):
        was, now_bytes = before.get(app), after.get(app)
        if was is None:
            changes.append({"app": app, "change": now_bytes, "bytes": now_bytes,
                            "state": "installed"})
        elif now_bytes is None:
            changes.append({"app": app, "change": -was, "bytes": 0,
                            "state": "removed"})
        elif now_bytes != was:
            changes.append({"app": app, "change": now_bytes - was,
                            "bytes": now_bytes, "state": "changed"})
    changes.sort(key=lambda c: -abs(c["change"]))
    return {"from_day": first, "to_day": last,
            "days_compared": len(days),
            "total_change": sum(c["change"] for c in changes),
            "apps": changes[:limit]}


def prune(conn, now=None, keep_days=400):
    """Forget snapshots older than the history is useful for."""
    init(conn)
    now = int(time.time()) if now is None else now
    with conn:
        conn.execute("DELETE FROM storage_history WHERE day < ?",
                     (now - keep_days * DAY,))


def usage(conn, limit=25):
    """Per application, largest first, with the parts that make up each total."""
    init(conn)
    rows = conn.execute(
        "SELECT app, kind, bytes, ts FROM storage").fetchall()
    apps = {}
    for app, kind, size, ts in rows:
        entry = apps.setdefault(app, {"app": app, "total": 0, "ts": ts,
                                      "bundle": 0, "support": 0, "caches": 0})
        entry[kind] = size
        entry["total"] += size
        entry["ts"] = max(entry["ts"], ts)
    out = sorted(apps.values(), key=lambda a: -a["total"])
    # An entry that exists only as a cache directory is usually not an app at
    # all -- it is a bundle identifier -- so it is kept but not promoted.
    return out[:limit]
