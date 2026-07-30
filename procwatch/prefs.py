"""Settings the recorder has to be able to read.

Most of the dashboard's preferences live in the browser's localStorage, which
is right: chart height and which cards are hidden describe how one person likes
to look at the data, and the recorder has no business knowing them.

These two are different. They decide what the *recorder* does -- whether it
works out what happened at all, and whether it puts a notification on screen
when it finds something -- and a launchd agent cannot read a browser's storage.
So they are in the database, which is the one thing both halves can see.

Deliberately a tiny typed store rather than a settings framework: two keys, a
default each, and a validator, so a hand-edited value or a stale row cannot
make the sampler throw on a tick.
"""
import time

DDL = """
CREATE TABLE IF NOT EXISTS pref (
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL,
  set_ts INTEGER NOT NULL
);
"""

# What to be told about when the diagnosis finds something.
#
#   causes  the findings that name a probable cause -- the default, because
#           these are the ones somebody would want interrupting for
#   all      every finding, including the ones that only cost something
#   off      nothing; the dashboard still shows them
NOTIFY_CHOICES = ("off", "causes", "all")

DEFAULTS = {
    # Whether to work out what happened at all. Off means no verdict, no
    # findings and no notifications -- and the sampler skips the work.
    "findings_enabled": "1",
    "findings_notify": "causes",
}

CHOICES = {
    "findings_enabled": ("0", "1"),
    "findings_notify": NOTIFY_CHOICES,
}


def init(conn):
    with conn:
        conn.executescript(DDL)


def get(conn, key):
    """The stored value, or the default -- never a surprise.

    A value that is not one of the choices is treated as absent rather than
    passed on. The alternative is a typo in the database deciding whether the
    recorder does its work.
    """
    if key not in DEFAULTS:
        raise KeyError(key)
    try:
        row = conn.execute("SELECT value FROM pref WHERE key = ?",
                           (key,)).fetchone()
    except Exception:
        return DEFAULTS[key]
    if row and row[0] in CHOICES[key]:
        return row[0]
    return DEFAULTS[key]


def set(conn, key, value):
    """Store one setting. Returns what was stored.

    The key check is deliberately explicit even though CHOICES[key] a few lines
    down would raise the same KeyError -- a mutation run confirmed the two are
    equivalent. It stays because a public setter should refuse a bad argument at
    its first line rather than incidentally, four conditions in.
    """
    if key not in DEFAULTS:
        raise KeyError(key)
    value = str(value)
    if value in ("true", "True"):
        value = "1"
    if value in ("false", "False"):
        value = "0"
    if value not in CHOICES[key]:
        raise ValueError("%s must be one of %s" % (key, ", ".join(CHOICES[key])))
    init(conn)
    with conn:
        conn.execute("INSERT OR REPLACE INTO pref (key, value, set_ts) "
                     "VALUES (?,?,?)", (key, value, int(time.time())))
    return value


def all_prefs(conn):
    return {key: get(conn, key) for key in DEFAULTS}


def findings_on(conn):
    return get(conn, "findings_enabled") == "1"
