"""Everything that happened, as opposed to everything that was measured.

The rest of this program samples: it asks the machine what it is doing now and
writes down the numbers. That answers "how much" and it cannot answer "what
happened". A number does not know that the Mac restarted at 22:17, that the
restart followed a shutdown that never completed, that Arc has been reported
to macOS for using more CPU than it is allowed forty-three times, or that a
macOS security update landed nine minutes before the machine got slow.

Those are events, and macOS records them in half a dozen unrelated places that
nothing reads together:

  ~/Library/Logs/DiagnosticReports   crashes, hangs, spins, resource reports
  /Library/Logs/DiagnosticReports    the same, for system processes
  sysctl kern.boottime + last        boots, shutdowns, logins
  system_profiler SPInstallHistory   installs and OS updates
  pmset -g log                       sleep and wake (already read by power.py)
  this database                      alerts, app versions, recording gaps

Reading them together is the whole point. Any one of them is a log; together
they are a history, and a history can be reasoned about: this event has
happened six times and always within an hour of waking; that one has never
happened before; these four happened inside ninety seconds and are obviously
one incident rather than four.

Three rules, the same three the diagnosis follows.

Collect cheaply and often, never expensively. Every source here is under a
tenth of a second, because a sampler that runs every thirty seconds cannot
afford a source that takes ten.

Idempotent. Each event has a stable key derived from what it is and when it
happened, so re-reading the same directory twice records nothing twice. That
is what makes it safe to collect on every tick and to reimport freely.

Never invent. An event is a thing macOS wrote down. Where this file draws a
conclusion -- that a shutdown was unclean, that a repeat is periodic -- it is
derived in the open from events that are themselves visible.
"""
import calendar
import json
import os
import re
import sqlite3
import subprocess
import time

from . import knowledge

DDL = """
CREATE TABLE IF NOT EXISTS event (
  key      TEXT PRIMARY KEY,
  ts       INTEGER NOT NULL,
  kind     TEXT NOT NULL,
  subject  TEXT NOT NULL DEFAULT '',
  detail   TEXT NOT NULL DEFAULT '',
  severity TEXT NOT NULL DEFAULT 'note',
  source   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS event_ts ON event (ts);
CREATE INDEX IF NOT EXISTS event_kind ON event (kind, subject, ts);

CREATE TABLE IF NOT EXISTS event_state (
  key   TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);
"""

# Where macOS files reports. The user directory holds their own applications;
# the system one holds daemons, and is readable without privileges.
REPORT_DIRS = (
    os.path.expanduser("~/Library/Logs/DiagnosticReports"),
    "/Library/Logs/DiagnosticReports",
)

# The extension carries the kind. Ordered most specific first, because the
# chain reads "Arc_2026-07-27-142829_host.cpu_resource.diag" -- the meaningful
# part is the second-to-last component, and a plain .diag is something else.
REPORT_KINDS = (
    ("panic", "panic", "fault"),
    ("shutdownstall", "shutdown-stall", "fault"),
    ("cpu_resource", "cpu-limit", "cost"),
    ("wakeups_resource", "wakeups-limit", "cost"),
    ("diskwrites_resource", "disk-limit", "cost"),
    ("hang", "hang", "fault"),
    ("spin", "spin", "fault"),
    ("ips", "crash", "fault"),
    ("crash", "crash", "fault"),
)

# Reports that are macOS talking to itself. proactive_event_tracker files land
# daily and mean nothing to anybody; a timeline containing one entry a day that
# is never worth reading is a timeline nobody opens.
REPORT_NOISE = ("proactive_event_tracker", "anon_system_stats",
                "DiagnosticLogs", "system_stats", "stacks-")

_REPORT = re.compile(r"^(?P<name>.+?)_(?P<stamp>\d{4}-\d{2}-\d{2}-\d{6})_")

# Packages Apple installs behind your back, several times a month. Worth
# recording -- a malware definition update is a real event -- but not worth
# ranking beside a kernel panic.
QUIET_PACKAGES = ("XProtect", "MRTConfigData", "Gatekeeper", "GatekeeperCompat",
                  "CoreLSKD", "RosettaUpdateAuto", "TCCConfigData",
                  "EligibilityConfigData", "AutoUpdate", "Config")

# Two events this close together are one incident. Chosen from what the machine
# actually produces: a wake, the backup it triggers and the indexing that
# follows arrive within a couple of minutes of each other, and reporting them
# as three unrelated things is how a timeline becomes noise.
EPISODE_GAP = 10 * 60

# A repeat has to happen this many times before "it keeps happening" is a
# claim rather than a coincidence.
MIN_REPEATS = 3

# Reports and installs move slowly; there is no point stat-ing two directories
# every thirty seconds.
COLLECT_EVERY = 15 * 60

# How closely repeats must cluster in the day before they are called a time of
# day rather than a scatter.
SAME_TIME_HOURS = 2.0

# What each kind means, in the words somebody would want. The severity ordering
# lives here too, so one table decides both how an event reads and how it ranks.
MEANINGS = {
    "panic": ("Your Mac crashed and restarted itself",
              "A kernel panic. The operating system itself failed, not an app. "
              "Almost always a driver, an external device, or failing memory."),
    "unclean-shutdown": ("Your Mac shut down without being asked to",
                         "It powered off without going through shutdown. A "
                         "held power button, a flat battery, a panic, or a "
                         "hardware fault."),
    "shutdown-stall": ("Your Mac took too long to shut down",
                       "Something refused to quit and macOS gave up waiting. "
                       "The report names what was still running."),
    "crash": ("crashed",
              "The program stopped unexpectedly. macOS wrote a report about "
              "it whether or not you saw a window."),
    "hang": ("stopped responding",
             "It stopped answering for long enough that macOS recorded what "
             "it was waiting for."),
    "spin": ("froze",
             "It was unresponsive and macOS captured what it was stuck on."),
    "cpu-limit": ("used more CPU than macOS allows",
                  "macOS gives background work a CPU budget and files a report "
                  "when something runs past it. Nothing is stopped -- but a "
                  "program that does this repeatedly is burning battery for "
                  "work it should not need."),
    "wakeups-limit": ("woke the CPU too often",
                      "Waking an idle CPU thousands of times a second costs "
                      "battery even when the work itself is trivial. macOS "
                      "files a report when a program does it excessively."),
    "disk-limit": ("wrote to the disk more than macOS allows",
                   "A background process wrote enough to be reported. On an "
                   "SSD this is wear as well as slowness."),
    "boot": ("Your Mac started up", ""),
    "shutdown": ("Your Mac shut down", ""),
    "login": ("You logged in", ""),
    "install": ("was installed", ""),
    "os-update": ("macOS was updated",
                  "Expect the machine to be slow for a while afterwards: "
                  "Spotlight reindexes, Photos reanalyses, and caches rebuild."),
    "security-update": ("Apple updated its malware definitions",
                        "Routine, silent, and several times a month."),
    "app-update": ("was updated", ""),
    "procwatch-update": ("was updated",
                         "Procwatch itself -- the recorder and this "
                         "dashboard. Recorded the first time the new "
                         "version ran."),
    "alert": ("crossed a limit you set", ""),
    "gap": ("Nothing was recorded",
            "The recorder was not running -- the Mac was off, asleep, or "
            "Procwatch was stopped."),
    "sleep": ("Your Mac went to sleep", ""),
    "wake": ("Your Mac woke up", ""),
}

SEVERITY_ORDER = {"fault": 0, "cost": 1, "change": 2, "note": 3}

# Things that happen because a laptop is a laptop. They belong on the timeline
# -- a crash one second after a wake is a different crash -- but an episode
# made only of these is not an incident, and a digest that leads with "your Mac
# went to sleep" has buried whatever it was meant to report.
ROUTINE = ("sleep", "wake", "boot", "shutdown", "login", "security-update",
           "gap")

# Events about the machine rather than about a program. Whatever filename
# the report carried, naming it here only clutters the sentence.
WHOLE_MACHINE = ("panic", "unclean-shutdown", "shutdown-stall", "boot",
                 "shutdown", "sleep", "wake", "gap", "login")


def init(conn):
    with conn:
        conn.executescript(DDL)


def _stamp(text):
    """A report filename's timestamp, which is local time with no zone.

    Read through mktime with isdst left at -1 so the platform resolves the
    offset that was in force on that date. Assuming the current offset would
    put every report from the other side of a clock change an hour out.
    """
    try:
        parts = time.strptime(text, "%Y-%m-%d-%H%M%S")
    except ValueError:
        return None
    return int(time.mktime((parts.tm_year, parts.tm_mon, parts.tm_mday,
                            parts.tm_hour, parts.tm_min, parts.tm_sec,
                            0, 0, -1)))


def _kind_of(suffixes):
    for needle, kind, severity in REPORT_KINDS:
        for suffix in suffixes:
            if suffix.lower() == needle:
                return kind, severity
    return None, None


def read_reports(dirs=None):
    """Every crash, hang, spin and resource report macOS has filed.

    Read from the filenames alone. The reports themselves are tens of
    kilobytes of stack traces, and the filename already carries the three
    things a timeline needs -- what, when, and what kind -- so opening them
    would cost a hundred times more for nothing.
    """
    out = []
    for folder in (dirs or REPORT_DIRS):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            match = _REPORT.match(name)
            if not match:
                continue
            subject = match.group("name")
            if any(noise in subject or noise in name for noise in REPORT_NOISE):
                continue
            ts = _stamp(match.group("stamp"))
            if ts is None:
                continue
            kind, severity = _kind_of(name[match.end():].split("."))
            if kind is None:
                continue
            out.append({"ts": ts, "kind": kind, "subject": subject,
                        "detail": "", "severity": severity,
                        "source": "report",
                        "key": "report:%s:%s:%s" % (kind, subject, ts)})
    return out


def _run(argv, timeout=10):
    try:
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout or ""


_LAST = re.compile(
    r"^(?P<who>\S+)\s+(?P<where>\S+)?\s*.*?"
    r"(?P<when>[A-Z][a-z]{2} [A-Z][a-z]{2} [ \d]\d \d\d:\d\d)")


def _last_stamps(text, now):
    """Parse `last`, which prints a weekday and no year.

    The year has to be inferred, and the inference is the interesting part:
    entries come newest first, so each one must be no later than the one
    before it. Any that is has crossed a new year boundary, and its year comes
    down until the ordering holds. Assuming the current year outright puts
    every entry from last December into next December.
    """
    out, ceiling = [], now + 86400
    for line in text.splitlines():
        match = _LAST.match(line)
        if not match:
            continue
        stamp = match.group("when")
        year = time.localtime(ceiling).tm_year
        ts = None
        for attempt in range(3):
            try:
                parts = time.strptime("%s %d" % (stamp, year - attempt),
                                      "%a %b %d %H:%M %Y")
            except ValueError:
                continue
            candidate = int(time.mktime((parts.tm_year, parts.tm_mon,
                                         parts.tm_mday, parts.tm_hour,
                                         parts.tm_min, 0, 0, 0, -1)))
            if candidate <= ceiling:
                ts = candidate
                break
        if ts is None:
            continue
        ceiling = ts
        out.append((line.split()[0], match.group("where") or "", ts))
    return out


def read_boots(now=None, reboot_text=None, boottime=None, corroborate=()):
    """Boots and shutdowns, and whether a shutdown was recorded before each.

    A boot with no shutdown logged before it is suggestive and not proof, and
    the difference matters. macOS does not reliably record a shutdown in wtmp,
    so "no shutdown was recorded" is partly a fact about the log; announcing
    every such boot as "your Mac shut down without being asked to" would be
    exactly the horoscope this project keeps refusing to write. The absence is
    reported as an absence, on the boot itself.

    It is promoted to a fault only when something independent agrees:
    `corroborate` carries the timestamps of panic and shutdown-stall reports,
    and a boot shortly after one of those is an unclean shutdown with evidence
    behind it rather than an inference from silence.

    The unified log does hold a definitive shutdown cause, and it is not used
    here: `log show` for that predicate ran past two minutes on the machine
    this was written on, against 13 ms for the sources above. A sampler that
    runs every thirty seconds cannot spend two minutes.
    """
    now = int(time.time()) if now is None else now
    text = reboot_text if reboot_text is not None else _run(["last", "reboot"])
    downs = _run(["last", "shutdown"]) if reboot_text is None else ""
    # Parsed separately, not concatenated. The year inference below leans on
    # entries arriving newest first, and joining two independently-ordered
    # listings puts an older entry in front of a newer one -- which the
    # inference reads as a year boundary and quietly dates it to last year.
    parsed = _last_stamps(text, now) + _last_stamps(downs, now)
    boots = [ts for who, _, ts in parsed if who == "reboot"]
    stops = [ts for who, _, ts in parsed if who == "shutdown"]

    # kern.boottime is the authority for the current boot: `last` rounds to the
    # minute and can disagree, and this one has to line up with sample
    # timestamps or the current session appears to begin a minute late.
    if boottime is None:
        found = re.search(r"sec\s*=\s*(\d+)", _run(["sysctl", "-n",
                                                    "kern.boottime"]))
        boottime = int(found.group(1)) if found else None
    if boottime:
        boots = [b for b in boots if abs(b - boottime) > 120] + [boottime]

    out = []
    for ts in sorted(set(stops)):
        out.append({"ts": ts, "kind": "shutdown", "subject": "", "detail": "",
                    "severity": "note", "source": "last",
                    "key": "shutdown:%s" % ts})
    for ts in sorted(set(boots)):
        clean = any(0 <= ts - stop <= 600 for stop in stops)
        witness = [w for w in corroborate if 0 <= ts - w <= 1200]
        out.append({"ts": ts, "kind": "boot", "subject": "",
                    "detail": "" if clean else
                              "No shutdown was recorded before this start, "
                              "which usually but not always means it was not "
                              "shut down cleanly.",
                    "severity": "note", "source": "last",
                    "key": "boot:%s" % ts})
        if not clean and witness:
            out.append({"ts": ts - 1, "kind": "unclean-shutdown", "subject": "",
                        "detail": "The Mac started at %s with no shutdown "
                                  "before it, and macOS filed a report %s "
                                  "earlier."
                                  % (time.strftime("%H:%M",
                                                   time.localtime(ts)),
                                     _duration(ts - max(witness))),
                        "severity": "fault", "source": "last+report",
                        "key": "unclean:%s" % ts})
    return out


def read_installs(now=None, text=None):
    """What has been installed, from macOS's own install history.

    Includes the updates nobody is told about: malware definitions, XProtect
    configuration, Rosetta. A machine that got slow an hour after a macOS
    update has an explanation sitting right here, and nothing else looks at it.
    """
    raw = text if text is not None else _run(
        ["system_profiler", "SPInstallHistoryDataType", "-json"], timeout=25)
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        return []
    items = parsed.get("SPInstallHistoryDataType") or []
    out = []
    for item in items:
        name = (item.get("_name") or "").strip()
        when = item.get("install_date") or ""
        if not name or not when:
            continue
        try:
            parts = time.strptime(when, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
        # The field is UTC, unlike every other timestamp in this file. mktime
        # would read it as local and put every install hours out; timegm is the
        # only one of the pair that means what this needs.
        ts = calendar.timegm(parts)
        version = (item.get("install_version") or "").strip()
        low = name.lower()
        if low.startswith("macos") or "mac os" in low:
            kind, severity = "os-update", "change"
        elif any(q.lower() in low for q in QUIET_PACKAGES):
            kind, severity = "security-update", "note"
        else:
            kind, severity = "install", "change"
        out.append({"ts": ts, "kind": kind, "subject": name,
                    "detail": ("version %s" % version) if version else "",
                    "severity": severity, "source": "install-history",
                    "key": "install:%s:%s" % (name, ts)})
    return out


def _each(conn, sql, args=()):
    """Query a table another module owns, tolerating its absence.

    power_event belongs to power.py, alert_event to alerts.py, app_version to
    versions.py. A database restored from an older backup, or one whose sampler
    has not reached those collectors yet, simply does not have them -- and a
    missing source has to cost its own events and nothing else. Without this,
    the first tick on a fresh database threw before recording anything.
    """
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def _from_database(conn, since=0):
    """Events this program already recorded, without knowing they were events.

    Sleep and wake, the alerts that fired, the app versions that changed and
    the stretches where nothing was recorded at all. Each was written for its
    own panel; on a timeline they are the connective tissue that makes the
    rest legible -- a crash means one thing at 15:00 and another one second
    after a wake.
    """
    out = []
    for ts, kind, reason, charge in _each(
            conn, "SELECT ts, kind, reason, charge FROM power_event "
                  "WHERE ts >= ?", (since,)):
        if kind not in ("sleep", "wake"):
            continue
        from . import power
        meaning = (power.describe_wake(reason) if kind == "wake"
                   else power.describe_sleep(reason))
        out.append({"ts": ts, "kind": kind, "subject": "",
                    "detail": meaning + ("" if charge < 0 else
                                         ", battery at %d%%" % charge),
                    "severity": "note", "source": "pmset",
                    "key": "%s:%s" % (kind, ts)})

    for ts_start, ts_end, reason in _each(
            conn, "SELECT ts_start, ts_end, reason FROM gap WHERE ts_end >= ?",
            (since,)):
        out.append({"ts": ts_start, "kind": "gap", "subject": "",
                    "detail": "%s, until %s%s"
                              % (_duration(ts_end - ts_start),
                                 time.strftime("%H:%M", time.localtime(ts_end)),
                                 " (%s)" % reason if reason else ""),
                    "severity": "note", "source": "sampler",
                    "key": "gap:%s" % ts_start})

    for ts, pattern, metric, value in _each(
            conn, "SELECT e.ts, r.pattern, r.metric, e.value FROM alert_event e "
                  "JOIN alert_rule r ON r.id = e.rule_id WHERE e.ts >= ?",
            (since,)):
        out.append({"ts": ts, "kind": "alert", "subject": pattern,
                    "detail": "%s reached %.0f" % (metric, value or 0),
                    "severity": "cost", "source": "alerts",
                    "key": "alert:%s:%s" % (pattern, ts)})

    # An application whose recorded version changed. The row that carries the
    # new version is the event; the first row for an app is its discovery, not
    # an update, so it is skipped.
    seen = {}
    for app, version, first_ts in _each(
            conn, "SELECT app, version, first_ts FROM app_version "
                  "ORDER BY app, first_ts"):
        # Procwatch's own updates come from self_version below, which also
        # covers the installs this scanner never sees. Reporting them from
        # here too put the same update on the timeline twice.
        if app == "Procwatch":
            continue
        if app in seen and version != seen[app] and first_ts >= since:
            out.append({"ts": first_ts, "kind": "app-update", "subject": app,
                        "detail": "%s to %s" % (seen[app], version),
                        "severity": "change", "source": "versions",
                        "key": "appver:%s:%s" % (app, first_ts)})
        seen[app] = version

    # Procwatch's own updates, recorded by selfupdate the first time a new
    # version runs. Same discovery rule as above: the first version ever seen
    # is an install, not an update.
    prior = None
    for version, first_ts in _each(
            conn, "SELECT version, first_ts FROM self_version "
                  "ORDER BY first_ts"):
        if prior is not None and version != prior and first_ts >= since:
            out.append({"ts": first_ts, "kind": "procwatch-update",
                        "subject": "Procwatch",
                        "detail": "%s to %s" % (prior, version),
                        "severity": "change", "source": "selfupdate",
                        "key": "selfver:%s:%s" % (version, first_ts)})
        prior = version
    return out


def _duration(seconds):
    seconds = int(seconds or 0)
    if seconds >= 7200:
        return "%.1f hours" % (seconds / 3600.0)
    if seconds >= 120:
        return "%d minutes" % (seconds // 60)
    return "%d seconds" % seconds


def _store(conn, rows):
    if not rows:
        return 0
    with conn:
        before = conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]
        conn.executemany(
            "INSERT OR IGNORE INTO event (key, ts, kind, subject, detail, "
            "severity, source) VALUES (?,?,?,?,?,?,?)",
            [(r["key"], int(r["ts"]), r["kind"], r["subject"], r["detail"],
              r["severity"], r["source"]) for r in rows])
        after = conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    return after - before


def due(conn, now=None, every=COLLECT_EVERY):
    now = int(time.time()) if now is None else now
    row = conn.execute("SELECT value FROM event_state WHERE key='collected'"
                       ).fetchone()
    return not row or (now - row[0]) >= every


def collect(conn, now=None, external=True):
    """Read every source and store what is new. Returns how many events were new.

    Safe to call as often as anybody likes: each event's key is derived from
    what it is and when it happened, so a second read of the same directory
    inserts nothing. `external=False` restricts it to the database, for tests
    and for a machine where shelling out is unwelcome.
    """
    now = int(time.time()) if now is None else now
    rows = list(_from_database(conn))
    if external:
        reports = read_reports()
        rows += reports
        # Boots read the reports rather than the reverse: whether a restart was
        # unclean is only claimable when a panic or a stalled shutdown backs it.
        rows += read_boots(now, corroborate=[
            r["ts"] for r in reports
            if r["kind"] in ("panic", "shutdown-stall")])
        rows += read_installs(now)
    added = _store(conn, rows)
    with conn:
        conn.execute("INSERT OR REPLACE INTO event_state (key, value) "
                     "VALUES ('collected', ?)", (now,))
    return added


# ---------------------------------------------------------------------------
# Reading the history, as opposed to recording it.
#
# Everything above turns six scattered sources into one table. Everything below
# is the part that makes the table worth having: an event on its own is a log
# line, and a log is something people install and never open. What they will
# open is a page that says these four things were one incident, this one has
# happened forty-three times and always after waking, and this one has never
# happened before.
# ---------------------------------------------------------------------------

def _headline(row):
    """One line for one event, in the words somebody would use.

    Kinds with a subject read as "Arc crashed"; kinds without read as "Your Mac
    started up". The distinction is in MEANINGS, so the phrasing of an event
    and its ranking are decided by the same table and cannot drift apart.
    """
    title, _ = MEANINGS.get(row["kind"], (row["kind"].replace("-", " "), ""))
    if not row["subject"] or row["kind"] in WHOLE_MACHINE:
        return title
    if title[:1].isupper():
        return "%s (%s)" % (title, row["subject"])
    return "%s %s" % (row["subject"], title)


def _rows(conn, start, end, kinds=None, limit=400):
    sql = ("SELECT key, ts, kind, subject, detail, severity, source FROM event "
           "WHERE ts >= ? AND ts < ?")
    args = [int(start), int(end)]
    if kinds:
        sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        args.extend(kinds)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(int(limit))
    cols = ("key", "ts", "kind", "subject", "detail", "severity", "source")
    return [dict(zip(cols, row)) for row in conn.execute(sql, args)]


def _busy_at(conn, ts, window=90, limit=3):
    """What was using the machine when this happened.

    The reason to keep samples and events in one database. "Arc crashed at
    18:47" is a log line; "Arc crashed at 18:47, while it was holding 190% of a
    core and swap was growing" is an explanation, and no amount of reading the
    crash report would produce it.
    """
    rows = conn.execute(
        "SELECT COALESCE(NULLIF(p.app, ''), p.exe) AS name, "
        "       MAX(s.cpu_max)/10.0 AS cpu "
        "FROM sample_raw s JOIN proc p ON p.id = s.proc_id "
        "WHERE s.ts BETWEEN ? AND ? GROUP BY name ORDER BY cpu DESC LIMIT ?",
        (int(ts) - window, int(ts) + window, limit)).fetchall()
    return [{"name": r[0], "cpu": round(r[1] or 0.0, 1)}
            for r in rows if (r[1] or 0) >= 10.0]


def timeline(conn, start, end, limit=200, context=True):
    """The events in a window, newest first, each explained.

    Faults and costs carry what was busy at the time; notes do not. Attaching
    context to a sleep event would be forty extra queries to tell somebody that
    their Mac was idle when it went to sleep.
    """
    out = []
    for row in _rows(conn, start, end, limit=limit):
        title, why = MEANINGS.get(row["kind"], ("", ""))
        row["headline"] = _headline(row)
        row["meaning"] = why
        row["about"] = (knowledge.describe(row["subject"])
                        if row["subject"] and row["kind"] in
                        ("crash", "hang", "spin", "cpu-limit", "wakeups-limit",
                         "disk-limit") else None)
        row["busy"] = (_busy_at(conn, row["ts"])
                       if context and row["severity"] in ("fault", "cost")
                       else [])
        out.append(row)
    return out


def episodes(conn, start, end, limit=400, routine=True):
    """Events grouped into incidents.

    A wake, the backup it starts, the indexing that follows and the battery it
    costs are one thing that happened, not four. Grouping is by gap rather than
    by fixed buckets, so an incident is however long it actually was, and a
    quiet hour produces no episode at all instead of an empty bucket.
    """
    rows = sorted(_rows(conn, start, end, limit=limit), key=lambda r: r["ts"])
    groups = []
    for row in rows:
        if groups and row["ts"] - groups[-1]["end"] <= EPISODE_GAP:
            groups[-1]["events"].append(row)
            groups[-1]["end"] = row["ts"]
        else:
            groups.append({"start": row["ts"], "end": row["ts"],
                           "events": [row]})

    for group in groups:
        events = group["events"]
        # Titled by the worst non-routine event where there is one. A wake
        # and a crash inside the same minute is "Arc crashed", not "your Mac
        # woke up" -- and both are severity note and fault respectively, so
        # ranking alone would get this right only by luck.
        worst = min(events, key=lambda e: (e["kind"] in ROUTINE,
                                           SEVERITY_ORDER.get(e["severity"], 9)))
        group["severity"] = worst["severity"]
        group["headline"] = _headline(worst)
        group["count"] = len(events)
        # What else was in the incident, named rather than counted. "and 3 other
        # events" is the kind of summary that makes somebody click to find out
        # it was nothing.
        rest = sorted((e for e in events if e is not worst),
                      key=lambda e: (e["kind"] in ROUTINE,
                                     SEVERITY_ORDER.get(e["severity"], 9),
                                     e["ts"]))
        others = [_headline(e) for e in rest]
        group["also"] = others[:4]
        group["more"] = max(0, len(others) - 4)
    if not routine:
        groups = [g for g in groups
                  if any(e["kind"] not in ROUTINE for e in g["events"])]
    groups.sort(key=lambda g: -g["end"])
    return groups


def _spread_hours(stamps):
    """How tightly a set of timestamps clusters in the day, in hours.

    Circular, because 23:50 and 00:10 are twenty minutes apart and a naive
    spread calls them nearly a day. Done by rotating the sorted hours to
    whichever starting point gives the smallest span -- the same trick as
    finding the largest gap on a clock face.
    """
    # Local hours. "Always around 03:00" is a claim about the clock on the
    # wall, and ts % 86400 is the hour in UTC -- which on this machine would
    # have reported a 03:00 pattern as 10:00.
    hours = sorted((time.localtime(ts).tm_hour + time.localtime(ts).tm_min / 60.0)
                   for ts in stamps)
    if len(hours) < 2:
        return 0.0
    best = 24.0
    for i in range(len(hours)):
        first = hours[i]
        span = max((h - first) % 24 for h in hours)
        best = min(best, span)
    return best


def _typical_hour(stamps):
    """The hour these cluster in, averaged the long way round if they straddle
    midnight -- otherwise 23:30 and 00:30 average to noon."""
    hours = [time.localtime(ts).tm_hour + time.localtime(ts).tm_min / 60.0
             for ts in stamps]
    base = hours[0]
    shifted = [((h - base + 12) % 24) - 12 for h in hours]
    return int(round(base + sum(shifted) / len(shifted))) % 24


def patterns(conn, start, end, min_repeats=MIN_REPEATS):
    """Events that keep happening, and what they keep happening with.

    This is the part a person cannot do by reading a list. Forty-three CPU
    reports scrolling past look like noise; "siriactionsd, twenty-two times,
    always within twenty minutes of waking" is a finding, and it was in the
    noise the whole time.
    """
    rows = sorted(_rows(conn, start, end, limit=2000), key=lambda r: r["ts"])
    wakes = [r["ts"] for r in rows if r["kind"] == "wake"]
    boots = [r["ts"] for r in rows if r["kind"] == "boot"]

    groups = {}
    for row in rows:
        # Changes are not repeats. "macOS was updated three times" is true, and
        # putting it beside "WhatsApp crashed nine times" is a category error --
        # one is a thing that went wrong repeatedly, the other is Tuesday.
        if row["kind"] in ROUTINE or row["severity"] in ("change", "note"):
            continue
        groups.setdefault((row["kind"], row["subject"]), []).append(row)

    out = []
    for (kind, subject), items in groups.items():
        if len(items) < min_repeats:
            continue
        stamps = [i["ts"] for i in items]
        after_wake = sum(1 for ts in stamps
                         if any(0 <= ts - w <= 1200 for w in wakes))
        after_boot = sum(1 for ts in stamps
                         if any(0 <= ts - b <= 600 for b in boots))
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        spread = _spread_hours(stamps)
        # A burst is the shape of something failing and being restarted, which
        # reads completely differently from the same count spread over weeks:
        # three inside ten minutes.
        burst = any(stamps[i + 2] - stamps[i] <= 600
                    for i in range(len(stamps) - 2))
        out.append({
            "kind": kind, "subject": subject, "count": len(items),
            "first_ts": stamps[0], "last_ts": stamps[-1],
            "severity": items[0]["severity"],
            "headline": _headline(items[0]),
            "after_wake": after_wake, "after_boot": after_boot,
            "spread_hours": round(spread, 1),
            "typical_hour": (_typical_hour(stamps)
                             if spread <= SAME_TIME_HOURS else None),
            "median_gap": (sorted(gaps)[len(gaps) // 2] if gaps else 0),
            "burst": burst,
            "about": knowledge.describe(subject) if subject else None,
        })
    out.sort(key=lambda p: (SEVERITY_ORDER.get(p["severity"], 9), -p["count"]))
    return out


def _ago(seconds):
    seconds = int(seconds or 0)
    if seconds < 3600:
        return "in the last hour"
    if seconds < 172800:
        return "%d hours ago" % (seconds // 3600)
    return "%d days ago" % (seconds // 86400)


def describe_pattern(pattern, now=None):
    """A repeat, in a sentence, saying only what the numbers support."""
    now = int(time.time()) if now is None else now
    span = pattern["last_ts"] - pattern["first_ts"]
    bits = ["%d times" % pattern["count"]]
    if span >= 86400:
        bits.append("over %d days" % max(1, span // 86400))
    bits.append("most recently %s" % _ago(now - pattern["last_ts"]))
    if pattern["burst"]:
        bits.append("several of them minutes apart, which is the shape of "
                    "something failing and being restarted")
    if pattern["count"] and pattern["after_wake"] >= max(2,
                                                         pattern["count"] // 2):
        bits.append("%d of them within twenty minutes of the Mac waking"
                    % pattern["after_wake"])
    elif pattern["count"] and pattern["after_boot"] >= max(2,
                                                           pattern["count"] // 2):
        bits.append("%d of them just after a restart" % pattern["after_boot"])
    if pattern["typical_hour"] is not None and pattern["count"] >= MIN_REPEATS:
        bits.append("almost always around %02d:00" % pattern["typical_hour"])
    return ", ".join(bits) + "."


# Before "this has never happened before" is a claim, the history has to reach
# back far enough to have caught it if it had. macOS prunes its own reports, so
# a kind whose oldest surviving record is four days old cannot support the
# sentence at all.
FIRST_NEEDS_HISTORY = 7 * 86400


def firsts(conn, start, end):
    """Events in this window of a kind that has never been seen before.

    The most under-reported fact in any monitoring tool. The fortieth time a
    process is reported for CPU is background noise; the first time is news,
    and on a list sorted by time the two look identical.

    Guarded, because the obvious implementation is wrong in a way that is
    invisible for the first week: with nothing older to compare against, every
    event is its own first occurrence, and a freshly installed Procwatch
    announces forty firsts and looks ridiculous. So the claim requires the
    history for that kind to predate the event by a week -- long enough that an
    earlier one would have been caught.
    """
    depth = {}
    out = []
    for row in _rows(conn, start, end, limit=400):
        if row["kind"] in ROUTINE or row["kind"] in ("install", "app-update"):
            continue
        if row["kind"] not in depth:
            depth[row["kind"]] = conn.execute(
                "SELECT MIN(ts) FROM event WHERE kind = ?",
                (row["kind"],)).fetchone()[0] or row["ts"]
        if row["ts"] - depth[row["kind"]] < FIRST_NEEDS_HISTORY:
            continue
        earlier = conn.execute(
            "SELECT COUNT(*) FROM event WHERE kind = ? AND subject = ? "
            "AND ts < ?", (row["kind"], row["subject"], row["ts"])).fetchone()[0]
        if earlier == 0:
            row["headline"] = _headline(row)
            out.append(row)
    return out


def counts(conn, start, end):
    rows = conn.execute(
        "SELECT kind, COUNT(*) FROM event WHERE ts >= ? AND ts < ? "
        "GROUP BY kind", (int(start), int(end))).fetchall()
    return {kind: n for kind, n in rows}


def digest(conn, start, end):
    """The whole window in a paragraph, with the workings kept underneath.

    Written in the order a person cares about: what broke, what keeps
    happening, what changed, and what the machine was simply doing. A window
    where none of that applies says so, because "nothing happened" is a real
    answer and pretending otherwise is how a tool loses trust.
    """
    tally = counts(conn, start, end)
    repeats = patterns(conn, start, end)
    new = firsts(conn, start, end)
    faults = _rows(conn, start, end,
                   kinds=("panic", "unclean-shutdown", "shutdown-stall",
                          "crash", "hang", "spin"), limit=100)

    lines = []
    if faults:
        # Grouped by what happened as well as to whom. Counting by subject alone
        # produced "macOS once, shutdown_stall once and WhatsApp once" -- three
        # names, no verb, and no way to tell a crash from a kernel panic.
        by_event = {}
        for row in faults:
            by_event.setdefault(_headline(row), 0)
            by_event[_headline(row)] += 1
        lines.append("%s." % _join(
            "%s%s" % (text, "" if n == 1 else " (%d times)" % n)
            for text, n in sorted(by_event.items(), key=lambda kv: -kv[1])[:5]))
    limits = sum(tally.get(k, 0) for k in ("cpu-limit", "wakeups-limit",
                                           "disk-limit"))
    if limits:
        lines.append("macOS filed %d report%s about programs going past the "
                     "CPU, wake or disk budget it gives background work."
                     % (limits, "" if limits == 1 else "s"))
    if tally.get("boot"):
        starts = tally["boot"]
        unclean = tally.get("unclean-shutdown", 0)
        text = "%d start%s" % (starts, "" if starts == 1 else "s")
        if unclean and starts == 1:
            text += ", which was not preceded by a shutdown"
        elif unclean == 1:
            text += ", one of them not preceded by a shutdown"
        elif unclean:
            text += ", %d of them not preceded by a shutdown" % unclean
        lines.append(text + ".")
    changed = tally.get("os-update", 0) + tally.get("install", 0) + \
        tally.get("app-update", 0)
    if changed:
        lines.append("%d thing%s installed or updated." %
                     (changed, "" if changed == 1 else "s"))

    return {"start": int(start), "end": int(end), "counts": tally,
            "summary": " ".join(lines) or
                       "Nothing was recorded as happening in this window.",
            "patterns": repeats[:8], "firsts": new[:8],
            "episodes": episodes(conn, start, end, routine=False)[:20]}


def _join(names):
    names = list(names)
    if len(names) < 2:
        return names[0] if names else ""
    return "%s and %s" % (", ".join(names[:-1]), names[-1])


def prune(conn, keep_days=400, now=None):
    """Events are a few hundred bytes each and the most valuable thing here.

    Kept far longer than the samples they sit beside: knowing this is the
    fourth unclean shutdown this year is worth more than knowing what the CPU
    did last Tuesday, and costs a thousandth as much to store.
    """
    now = int(time.time()) if now is None else now
    with conn:
        conn.execute("DELETE FROM event WHERE ts < ?",
                     (now - keep_days * 86400,))
