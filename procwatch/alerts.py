"""Rules that watch the recording and say something when they are met.

procwatch records everything and never speaks. That is the gap this closes:
the question that started the tool -- "why is my Mac slow" -- is one it could
have answered before being asked, if anything had been watching.

A rule is deliberately small: a metric, a threshold, and how long it has to
hold. "Anything above 80% CPU for ten minutes" catches a runaway; "above 80%
for one sample" catches a compiler starting up, which is not news. Sustain is
what separates a problem from a spike, so it is not optional.

Evaluated by the recorder, once per tick, against rows it has just written.
Nothing here polls, and nothing needs the dashboard to be open.
"""
import json
import subprocess
import time

from . import config, db

# Metrics a rule can watch. Each maps to a column the sampler already writes,
# with the scale it is stored at, so a rule states its threshold in the unit a
# person would say out loud.
METRICS = {
    "cpu":    {"column": "cpu_avg",   "scale": 10.0,  "unit": "%"},
    "memory": {"column": "rss_avg",   "scale": 1024.0, "unit": "MB"},
    "disk":   {"column": "disk_read + disk_write", "scale": 1048576.0,
               "unit": "MB/s", "rate": True},
    "net":    {"column": "net_in + net_out", "scale": 1048576.0,
               "unit": "MB/s", "rate": True},
}

DDL = """
CREATE TABLE IF NOT EXISTS alert_rule (
  id        INTEGER PRIMARY KEY,
  pattern   TEXT NOT NULL,
  metric    TEXT NOT NULL,
  threshold REAL NOT NULL,
  sustain   INTEGER NOT NULL,
  enabled   INTEGER NOT NULL DEFAULT 1,
  added_ts  INTEGER NOT NULL,
  UNIQUE (pattern, metric, threshold, sustain)
);

CREATE TABLE IF NOT EXISTS alert_event (
  id        INTEGER PRIMARY KEY,
  rule_id   INTEGER NOT NULL,
  proc_id   INTEGER NOT NULL,
  ts        INTEGER NOT NULL,
  value     REAL NOT NULL,
  notified  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS alert_event_ts ON alert_event (ts);
"""

# Once a rule has fired for a process, it stays quiet about that process for
# this long. A runaway that lasts an hour is one piece of news, not 120.
REARM = 1800


def init(conn):
    with conn:
        conn.executescript(DDL)


def add(conn, pattern, metric, threshold, sustain):
    if metric not in METRICS:
        raise ValueError("unknown metric %r; try %s"
                         % (metric, ", ".join(sorted(METRICS))))
    if sustain < config.INTERVAL:
        raise ValueError("sustain must be at least one sample (%ds)"
                         % config.INTERVAL)
    init(conn)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO alert_rule "
            "(pattern, metric, threshold, sustain, added_ts) VALUES (?,?,?,?,?)",
            (pattern, metric, float(threshold), int(sustain), int(time.time())))
    return rules(conn)


def remove(conn, rule_id):
    init(conn)
    with conn:
        cur = conn.execute("DELETE FROM alert_rule WHERE id = ?", (rule_id,))
    return cur.rowcount > 0


def rules(conn):
    init(conn)
    return [dict(id=r[0], pattern=r[1], metric=r[2], threshold=r[3],
                 sustain=r[4], enabled=bool(r[5]))
            for r in conn.execute(
                "SELECT id, pattern, metric, threshold, sustain, enabled "
                "FROM alert_rule ORDER BY id").fetchall()]


def _matches(pattern, exe, app):
    """A rule's pattern against an identity.

    Substring, case-insensitive, and "*" means everything -- because the rule
    people actually want first is "tell me about anything", and making them
    learn a syntax to express it would be a poor trade.
    """
    if pattern == "*":
        return True
    needle = pattern.lower()
    return needle in (exe or "").lower() or needle in (app or "").lower()


def evaluate(conn, now=None):
    """Check every enabled rule against the samples just written.

    Returns the events raised. Reads only the raw tier and only the window a
    rule asks for, so the cost is bounded by the longest sustain rather than
    by the size of the history.
    """
    init(conn)
    now = int(time.time()) if now is None else now
    active = [r for r in rules(conn) if r["enabled"]]
    if not active:
        return []

    raised = []
    for rule in active:
        spec = METRICS[rule["metric"]]
        window = max(rule["sustain"], config.INTERVAL)
        start = now - window
        # Every sample in the window must be over the threshold, and there
        # must be enough of them to cover it -- otherwise a process that
        # reported once, highly, would look like it had sustained anything.
        needed = max(1, window // config.INTERVAL)
        rows = conn.execute(
            "SELECT proc_id, COUNT(*), MIN(%s), AVG(%s) "
            "FROM sample_raw WHERE ts > ? AND ts <= ? "
            "GROUP BY proc_id HAVING COUNT(*) >= ? AND MIN(%s) > ?"
            % (spec["column"], spec["column"], spec["column"]),
            (start, now, needed, rule["threshold"] * spec["scale"])).fetchall()
        if not rows:
            continue
        ids = [r[0] for r in rows]
        names = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT id, exe, app FROM proc WHERE id IN (%s)"
            % ",".join("?" * len(ids)), ids).fetchall()}
        for proc_id, _count, _low, mean in rows:
            exe, app = names.get(proc_id, ("", ""))
            if exe == config.OTHER or not _matches(rule["pattern"], exe, app):
                continue
            recent = conn.execute(
                "SELECT MAX(ts) FROM alert_event WHERE rule_id = ? AND proc_id = ?",
                (rule["id"], proc_id)).fetchone()[0]
            if recent and now - recent < REARM:
                continue
            value = mean / spec["scale"]
            with conn:
                conn.execute(
                    "INSERT INTO alert_event (rule_id, proc_id, ts, value) "
                    "VALUES (?,?,?,?)", (rule["id"], proc_id, now, value))
            raised.append({"rule": rule, "exe": exe, "app": app,
                           "ts": now, "value": value, "unit": spec["unit"]})
    return raised


def post(title, body):
    """Put one notification on screen, via the notification centre.

    osascript rather than a framework binding: it is the only way to post a
    macOS notification without a signed bundle, and this has to work when
    procwatch is a single Python file that was curled from the internet.

    Shared, because the diagnosis posts notifications too and two copies of
    this would eventually differ in how they quote a process name -- which is
    the one thing here that can turn a notification into a shell argument.
    json.dumps is doing that quoting: it produces an AppleScript string
    literal, and it is why a process called `foo" & do shell script "bad` is a
    name rather than a command.
    """
    try:
        subprocess.run(
            ["osascript", "-e",
             'display notification %s with title %s'
             % (json.dumps(body), json.dumps(title))],
            capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass          # a missed notification is not worth failing a tick over


def notify(event):
    """Put an alert on screen."""
    post("%s %s" % (event["exe"], event["rule"]["metric"]),
         "%s at %.1f%s for %s" % (
             event["app"] or event["exe"], event["value"], event["unit"],
             _duration(event["rule"]["sustain"])))


def _duration(seconds):
    if seconds >= 3600:
        return "%g hours" % round(seconds / 3600.0, 1)
    if seconds >= 60:
        return "%d minutes" % (seconds // 60)
    return "%d seconds" % seconds


def recent(conn, limit=50):
    init(conn)
    rows = conn.execute(
        "SELECT e.ts, e.value, p.exe, p.app, r.metric, r.threshold, r.sustain "
        "FROM alert_event e JOIN proc p ON p.id = e.proc_id "
        "JOIN alert_rule r ON r.id = e.rule_id "
        "ORDER BY e.ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(ts=r[0], value=r[1], exe=r[2], app=r[3], metric=r[4],
                 threshold=r[5], sustain=r[6],
                 unit=METRICS.get(r[4], {}).get("unit", ""))
            for r in rows]
