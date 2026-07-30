"""What each application's version was, and whether an update made it worse.

This is the one thing a monitor with no memory cannot do at all. "Chrome feels
slower since it updated" is among the most common suspicions people have about
their machines, and answering it needs two things kept side by side: how much
an application used, and which version was installed at the time.

The comparison is deliberately conservative. Software use is spiky -- a browser
with forty tabs open is not comparable to the same browser with three -- so a
difference is only reported when it is large, when both sides have enough
samples to mean something, and when the busier side is busy enough to matter.
Crying regression at a 12% wobble would make the feature worse than absent.
"""
import os
import plistlib
import time

from . import config

DDL = """
CREATE TABLE IF NOT EXISTS app_version (
  app        TEXT NOT NULL,
  version    TEXT NOT NULL,
  first_ts   INTEGER NOT NULL,
  last_ts    INTEGER NOT NULL,
  PRIMARY KEY (app, version)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS app_version_seen ON app_version (app, first_ts);
"""

ROOTS = ("/Applications", "/System/Applications",
         os.path.expanduser("~/Applications"))

# What counts as a regression worth telling someone about.
MIN_SAMPLES = 40          # about twenty minutes of recording on each side
MIN_RATIO = 1.5           # half again as much, not a wobble
MIN_CPU = 3.0             # below this, doubling is still nothing
MIN_MEMORY_MB = 200.0


def init(conn):
    with conn:
        conn.executescript(DDL)


def installed(roots=None):
    """Every application and the version it is at right now.

    Read from the bundle rather than from a running process: an app that is
    not open still has a version, and a version change should be noticed the
    next time the recorder runs rather than the next time you launch it.
    """
    found = {}
    for root in (roots or ROOTS):
        if not os.path.isdir(root):
            continue
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".app") or name.startswith("."):
                continue
            path = os.path.join(root, name, "Contents", "Info.plist")
            try:
                with open(path, "rb") as handle:
                    info = plistlib.load(handle)
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
            version = (info.get("CFBundleShortVersionString")
                       or info.get("CFBundleVersion"))
            if not version:
                continue
            found[name[:-4]] = str(version).strip()
    return found


def tick(conn, now=None, roots=None):
    """Note the version each application is at.

    A version already seen has its last_ts moved forward; a new one starts a
    row. So the history is a list of "this app was at this version between
    these times", which is exactly what a before-and-after needs.
    """
    init(conn)
    now = int(time.time()) if now is None else now
    rows = [(app, version, now, now) for app, version in
            installed(roots).items()]
    with conn:
        conn.executemany(
            "INSERT INTO app_version (app, version, first_ts, last_ts) "
            "VALUES (?,?,?,?) ON CONFLICT(app, version) DO UPDATE SET "
            "last_ts = excluded.last_ts", rows)
    return len(rows)


def updates(conn, since=None, now=None):
    """Applications that changed version in the window, newest first.

    An application seen at only one version has not updated -- the first
    sighting of something is not an update, and reporting it as one would
    label every app as changed on the day the tool was installed.
    """
    init(conn)
    now = int(time.time()) if now is None else now
    since = (now - 30 * 86400) if since is None else since
    out = []
    for app, in conn.execute(
            "SELECT DISTINCT app FROM app_version WHERE first_ts >= ? "
            "AND first_ts <= ?", (since, now)).fetchall():
        seen = conn.execute(
            "SELECT version, first_ts, last_ts FROM app_version WHERE app = ? "
            "ORDER BY first_ts", (app,)).fetchall()
        # Pairs, so an application seen at only one version yields nothing:
        # the first sighting of something is not an update, and treating it as
        # one would label every app as changed on the day this was installed.
        #
        # The window after an update ends when the NEXT version arrives, not at
        # now. Running it to now meant an old update was judged against every
        # version since: an application that crept up 300, 380, 460, 540, 620 MB
        # had its first step reported as 300 to 500 -- an average of the four
        # versions that followed -- and called a regression that no single step
        # was. `now` only applies to the newest version, which has nothing after
        # it yet.
        for i, (older, newer) in enumerate(zip(seen, seen[1:])):
            if newer[1] < since:
                continue
            next_up = seen[i + 2][1] if i + 2 < len(seen) else None
            after_end = next_up if next_up else max(newer[2], now)
            out.append({"app": app, "from_version": older[0],
                        "to_version": newer[0], "changed_ts": newer[1],
                        "before_start": older[1], "before_end": newer[1],
                        "after_end": after_end})
    out.sort(key=lambda u: -u["changed_ts"])
    return out


def _usage(conn, app, start, end):
    """Average CPU and memory for an application over a window.

    Averaged across the whole window rather than over the samples that exist,
    so a version that ran for ten minutes of a two-day window is not credited
    with the machine being idle for the rest of it. Only the raw tier is read:
    the comparison is between two recent periods, which is where raw samples
    live.
    """
    row = conn.execute(
        "SELECT AVG(s.cpu_avg), MAX(s.cpu_max), AVG(s.rss_avg), COUNT(*) "
        "FROM sample_raw s JOIN proc p ON p.id = s.proc_id "
        "WHERE p.app = ? AND s.ts >= ? AND s.ts < ?",
        (app, start, end)).fetchone()
    if not row or not row[3]:
        return None
    return {"cpu": (row[0] or 0) / 10.0, "cpu_peak": (row[1] or 0) / 10.0,
            "memory_mb": (row[2] or 0) / 1024.0, "samples": row[3]}


def _minutes(seconds):
    seconds = int(seconds or 0)
    if seconds < 90:
        return "%d seconds" % seconds
    return "%d minutes" % (seconds // 60)


def compared(conn, since=None, now=None):
    """Every update, with what happened to its usage -- or why that cannot be
    said yet.

    Split out from regressions() because the interface needs the updates that
    were NOT judged as much as the ones that were. Reporting only the ones that
    crossed a threshold produced a panel that said "1 update, none of which
    measurably changed how much the application uses" -- which withholds the two
    things the reader wanted, namely which application and which versions, and
    hides the difference between "measured, and nothing changed" and "not enough
    recorded yet to measure". Those are not the same answer, and only one of them
    is worth waiting for.

    One place decides the thresholds, and regressions() is expressed in terms of
    this -- two copies of MIN_SAMPLES would eventually disagree about what a
    finding is.
    """
    init(conn)
    now = int(time.time()) if now is None else now
    out = []
    for update in updates(conn, since=since, now=now):
        # How long the new version has been observed, so the window before the
        # update is the same length as the window after it.
        # Equal windows either side, so a version that has been current for ten
        # minutes is not compared against a fortnight of the one before it.
        span = update["after_end"] - update["changed_ts"]
        before = _usage(conn, update["app"],
                        max(update["before_start"], update["changed_ts"] - span),
                        update["changed_ts"])
        after = _usage(conn, update["app"], update["changed_ts"],
                       update["after_end"])
        row = dict(update)
        row["change"] = None
        seen = min(before["samples"] if before else 0,
                   after["samples"] if after else 0)
        row["samples"] = seen
        row["samples_needed"] = MIN_SAMPLES

        # Too soon is decided before too little: an update from a minute ago has
        # a window a minute wide on each side, which cannot hold forty samples
        # and will not hold any at all in the first thirty seconds. Reporting
        # that as "it was not running before the update" is wrong about a
        # process that was running the whole time -- as it was for Procwatch's
        # own update, thirty seconds after installing it.
        if span < MIN_SAMPLES * config.INTERVAL:
            row["state"] = "too-soon"
            row["why"] = ("only %s of the %d minutes needed on each side of the "
                          "update" % (_minutes(span),
                                      MIN_SAMPLES * config.INTERVAL // 60))
            out.append(row)
            continue

        if not before or not after:
            row["state"] = "unrecorded"
            # Which side is missing, because they mean different things: an app
            # with nothing recorded before was installed rather than updated as
            # far as this recording is concerned, and one with nothing after has
            # simply not been opened since.
            row["why"] = ("it was not running before the update, so there is "
                          "nothing to compare against"
                          if not before else
                          "it has not run since the update")
            out.append(row)
            continue
        if seen < MIN_SAMPLES:
            row["state"] = "too-soon"
            row["why"] = ("only %d of the %d samples needed on each side of the "
                          "update" % (seen, MIN_SAMPLES))
            out.append(row)
            continue

        # The largest real change across the metrics, rather than the first --
        # an update that halves the CPU and doubles the memory has one headline
        # and it is not whichever was checked first.
        best = None
        for metric, floor, unit in (("cpu", MIN_CPU, "%"),
                                    ("memory_mb", MIN_MEMORY_MB, "MB")):
            was, is_now = before[metric], after[metric]
            if max(was, is_now) < floor or was <= 0:
                continue
            ratio = is_now / was
            if MIN_RATIO > ratio > 1.0 / MIN_RATIO:
                continue
            found = {
                "app": update["app"], "metric": metric, "unit": unit,
                "from_version": update["from_version"],
                "to_version": update["to_version"],
                "changed_ts": update["changed_ts"],
                "before": round(was, 1), "after": round(is_now, 1),
                "ratio": round(ratio, 2),
                "worse": ratio > 1.0,
                "samples": seen,
            }
            if best is None or abs(ratio - 1.0) > abs(best["ratio"] - 1.0):
                best = found
        if best is None:
            row["state"] = "same"
            row["why"] = "no change big enough to be worth reporting"
            # What was measured, so "nothing changed" is a claim with numbers
            # under it rather than a shrug.
            row["cpu"] = [round(before["cpu"], 1), round(after["cpu"], 1)]
            row["memory_mb"] = [round(before["memory_mb"], 1),
                                round(after["memory_mb"], 1)]
        else:
            row["state"] = "worse" if best["worse"] else "better"
            row["change"] = best
            row["why"] = ""
        out.append(row)
    return out


def regressions(conn, since=None, now=None):
    """Updates that cost you something, with the numbers behind the claim.

    Both directions are reported: an update that made an application lighter
    is worth knowing too, and a feature that only ever finds bad news reads as
    a tool looking for something to complain about.
    """
    found = [row["change"] for row in compared(conn, since=since, now=now)
             if row["change"]]
    found.sort(key=lambda f: -abs(f["ratio"] - 1.0))
    return found


def history(conn, since=None, now=None):
    """Every version of every application, with what each one cost.

    The update list answers "did this update change anything", one step at a
    time. It cannot answer the question that follows it -- "is this application
    heavier than it was three versions ago" -- because each comparison only ever
    looks at the step it belongs to, and four steps of "no real change" can add
    up to twice the memory.

    So each version is measured over the span it was current, and they are
    returned in order. Any two can then be compared, and the last one can be
    compared with the first.
    """
    init(conn)
    now = int(time.time()) if now is None else now
    rows = conn.execute(
        "SELECT app, version, first_ts, last_ts FROM app_version "
        "ORDER BY app, first_ts").fetchall()

    by_app = {}
    for app, version, first_ts, last_ts in rows:
        by_app.setdefault(app, []).append(
            {"version": version, "first_ts": first_ts, "last_ts": last_ts})

    out = []
    for app, versions in sorted(by_app.items()):
        entries = []
        for i, entry in enumerate(versions):
            # A version is current until the next one appears, and the newest
            # is current until now. Measuring to last_ts instead would stop at
            # the last time the app was seen running, which for the newest
            # version is a moving target and for the others is the same thing.
            start = entry["first_ts"]
            end = (versions[i + 1]["first_ts"] if i + 1 < len(versions)
                   else max(now, entry["last_ts"]))
            usage = _usage(conn, app, start, end)
            entries.append({
                "version": entry["version"],
                "first_ts": start,
                "until_ts": end,
                "cpu": round(usage["cpu"], 1) if usage else None,
                "cpu_peak": round(usage["cpu_peak"], 1) if usage else None,
                "memory_mb": round(usage["memory_mb"], 1) if usage else None,
                "samples": usage["samples"] if usage else 0,
                "enough": bool(usage and usage["samples"] >= MIN_SAMPLES),
            })

        # First to last, which is the comparison the step-by-step list cannot
        # make: four updates that each changed nothing can still have doubled
        # the memory between them.
        measured = [e for e in entries if e["enough"]]
        drift = None
        if len(measured) >= 2:
            first, last = measured[0], measured[-1]
            drift = {"from_version": first["version"],
                     "to_version": last["version"],
                     "versions": len(measured)}
            for metric, floor in (("cpu", MIN_CPU),
                                  ("memory_mb", MIN_MEMORY_MB)):
                was, is_now = first[metric], last[metric]
                if not was or max(was, is_now) < floor:
                    continue
                ratio = is_now / was
                if MIN_RATIO > ratio > 1.0 / MIN_RATIO:
                    continue
                drift[metric] = {"before": was, "after": is_now,
                                 "ratio": round(ratio, 2),
                                 "worse": ratio > 1.0}
            if not any(k in drift for k in ("cpu", "memory_mb")):
                drift["same"] = True

        if since is None or entries[-1]["until_ts"] >= since:
            out.append({"app": app, "versions": entries, "drift": drift})
    return out


def prune(conn, now=None):
    init(conn)
    now = int(time.time()) if now is None else now
    keep = max([t.keep for t in config.TIERS if t.keep] or [365 * 86400])
    with conn:
        conn.execute("DELETE FROM app_version WHERE last_ts < ?", (now - keep,))
