"""Schema and connections. One writer (the sampler), many readers."""
import sqlite3

from . import config

_SAMPLE_DDL = """
CREATE TABLE IF NOT EXISTS sample_{tier} (
  ts          INTEGER NOT NULL,
  proc_id     INTEGER NOT NULL REFERENCES proc(id),
  cpu_avg     INTEGER NOT NULL,
  cpu_max     INTEGER NOT NULL,
  cpu_max_ts  INTEGER NOT NULL,
  rss_avg     INTEGER NOT NULL,
  rss_max     INTEGER NOT NULL,
  nproc       INTEGER NOT NULL,
  samples     INTEGER NOT NULL,
  net_in      INTEGER NOT NULL DEFAULT 0,
  net_out     INTEGER NOT NULL DEFAULT 0,
  disk_read   INTEGER NOT NULL DEFAULT 0,
  disk_write  INTEGER NOT NULL DEFAULT 0,
  energy      INTEGER NOT NULL DEFAULT 0,
  stuck       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (ts, proc_id)
) WITHOUT ROWID;
"""

# Byte and energy counters added after the first databases were already
# recording. ALTER TABLE ADD COLUMN is cheap in SQLite (metadata only) and
# backfills existing rows with the default, so old history keeps its CPU and
# memory and simply reports zero for metrics that were never collected.
_SAMPLE_COLUMNS = ("net_in", "net_out", "disk_read", "disk_write",
                   "energy", "stuck")

# Battery arrived later still. -1 means "not read", which is not 0.
_SYSTEM_COLUMNS = (("batt_pct", -1), ("batt_draw_mw", -1),
                   ("batt_full_mwh", 0), ("on_ac", 1))

_SYSTEM_DDL = """
CREATE TABLE IF NOT EXISTS system_{tier} (
  ts           INTEGER PRIMARY KEY,
  cpu_busy     INTEGER NOT NULL,
  load1        INTEGER NOT NULL,
  mem_used_kb  INTEGER NOT NULL,
  mem_comp_kb  INTEGER NOT NULL,
  swap_used_kb INTEGER NOT NULL,
  disk_free_kb INTEGER NOT NULL,
  samples      INTEGER NOT NULL,
  batt_pct     INTEGER NOT NULL DEFAULT -1,
  batt_draw_mw INTEGER NOT NULL DEFAULT -1,
  batt_full_mwh INTEGER NOT NULL DEFAULT 0,
  on_ac        INTEGER NOT NULL DEFAULT 1,
  expected     INTEGER NOT NULL
) WITHOUT ROWID;
"""

_SUPPORT_DDL = """
CREATE TABLE IF NOT EXISTS proc (
  id           INTEGER PRIMARY KEY,
  exe          TEXT NOT NULL,
  args_sig     TEXT NOT NULL,
  cmdline_full TEXT NOT NULL,
  is_system    INTEGER NOT NULL DEFAULT 1,
  app          TEXT NOT NULL DEFAULT '',
  UNIQUE (exe, args_sig)
);

CREATE TABLE IF NOT EXISTS watchlist (
  pattern  TEXT PRIMARY KEY,
  added_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS gap (
  ts_start INTEGER PRIMARY KEY,
  ts_end   INTEGER NOT NULL,
  reason   TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS sampler_extra (
  pid        INTEGER PRIMARY KEY,
  net_in     INTEGER NOT NULL,
  net_out    INTEGER NOT NULL,
  disk_read  INTEGER NOT NULL,
  disk_write INTEGER NOT NULL,
  energy     INTEGER NOT NULL,
  stuck_run  INTEGER NOT NULL DEFAULT 0,
  updated_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sampler_state (
  pid        INTEGER PRIMARY KEY,
  start_time INTEGER NOT NULL,
  cputime_cs INTEGER NOT NULL,
  updated_ts INTEGER NOT NULL
);
"""


def connect(path):
    conn = sqlite3.connect(path, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn):
    """Safe to call on every tick; every statement is IF NOT EXISTS."""
    with conn:
        conn.executescript(_SUPPORT_DDL)
        # Owned by their modules, registered here so every database has them
        # from the first tick rather than from whenever that module first runs.
        from . import diagnose, events, prefs
        conn.executescript(events.DDL)
        conn.executescript(prefs.DDL)
        conn.executescript(diagnose.DDL)
        for tier in config.TIERS:
            conn.executescript(_SAMPLE_DDL.format(tier=tier.name))
            conn.executescript(_SYSTEM_DDL.format(tier=tier.name))
        _migrate(conn)


def _backfill_proc_flags(conn):
    """Classify identities interned before the column existed.

    Done from the stored command line alone, which is all the history has --
    the parent chain that resolves a pathless process is only available at
    sampling time. Anything unresolvable stays flagged as system, matching the
    live classifier's own fallback.
    """
    from . import identity
    rows = conn.execute(
        "SELECT id, cmdline_full FROM proc WHERE is_system = 1").fetchall()
    updates = []
    for pid, cmdline in rows:
        verdict = identity.is_system((cmdline or "").split(" ")[0])
        if verdict is False:
            updates.append((0, pid))
    if updates:
        conn.executemany("UPDATE proc SET is_system = ? WHERE id = ?", updates)


def _migrate(conn):
    """Add columns a database created by an older version is missing.

    SQLite has no ADD COLUMN IF NOT EXISTS, so the existing columns are read
    back and only the absent ones are added. This runs on every tick, which is
    fine -- once the columns exist it is a single PRAGMA per table and no
    writes at all.
    """
    for tier in config.TIERS:
        table = "sample_" + tier.name
        have = set(row[1] for row in conn.execute(
            "PRAGMA table_info(%s)" % table).fetchall())
        for column in _SAMPLE_COLUMNS:
            if column not in have:
                conn.execute("ALTER TABLE %s ADD COLUMN %s INTEGER NOT NULL "
                             "DEFAULT 0" % (table, column))

        pcols = set(row[1] for row in conn.execute(
            "PRAGMA table_info(proc)").fetchall())
        if "app" not in pcols:
            conn.execute("ALTER TABLE proc ADD COLUMN app TEXT NOT NULL DEFAULT ''")
        if "is_system" not in pcols:
            conn.execute("ALTER TABLE proc ADD COLUMN is_system INTEGER "
                         "NOT NULL DEFAULT 1")
            _backfill_proc_flags(conn)

        stable = "system_" + tier.name
        shave = set(row[1] for row in conn.execute(
            "PRAGMA table_info(%s)" % stable).fetchall())
        for column, default in _SYSTEM_COLUMNS:
            if column not in shave:
                conn.execute("ALTER TABLE %s ADD COLUMN %s INTEGER NOT NULL "
                             "DEFAULT %d" % (stable, column, default))
