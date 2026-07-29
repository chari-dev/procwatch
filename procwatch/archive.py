"""Copying the database out, and putting one back.

A recorded history is the only thing here that cannot be regenerated. The
program can be reinstalled and the charts redrawn, but a year of samples
exists in exactly one file, so moving that file is worth doing properly
rather than with `cp`.

`cp` is in fact the specific thing to avoid. The sampler writes every thirty
seconds, and SQLite in WAL mode keeps recent writes in a side file; copying
the main database while a write is in flight produces a file that opens fine
and is missing or torn at the end. sqlite3's own backup API takes a
transactionally consistent snapshot of a live database, so that is what this
uses.
"""
import os
import shutil
import sqlite3
import time

from . import config, db

# A file has to look like ours before it is allowed to replace ours. These are
# the tables every procwatch database has had since the first version.
REQUIRED_TABLES = ("proc", "sample_raw", "system_raw", "sampler_state")


def _tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def looks_like_procwatch(path):
    """Whether this file is a readable database with our tables in it.

    Checked before a restore overwrites anything, because the failure mode
    otherwise is losing real history to a typo in a filename.
    """
    if not os.path.isfile(path):
        return False, "no such file: %s" % path
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    except sqlite3.Error as error:
        return False, "cannot open %s: %s" % (path, error)
    try:
        missing = [t for t in REQUIRED_TABLES if t not in _tables(conn)]
    except sqlite3.DatabaseError as error:
        return False, "%s is not a database: %s" % (path, error)
    finally:
        conn.close()
    if missing:
        return False, "%s is missing %s" % (path, ", ".join(missing))
    return True, ""


def default_name(now=None):
    stamp = time.strftime("%Y-%m-%d-%H%M", time.localtime(now or time.time()))
    return "procwatch-%s.db" % stamp


def backup(destination, source=None):
    """Snapshot the live database to `destination`.

    Returns the path written. A directory is accepted and gets a dated
    filename inside it, because `backup ~/Desktop` is what people type.
    """
    source = source or config.DB_PATH
    if not os.path.exists(source):
        raise RuntimeError("nothing to back up: %s does not exist" % source)
    if os.path.isdir(destination):
        destination = os.path.join(destination, default_name())
    parent = os.path.dirname(os.path.abspath(destination))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    live = sqlite3.connect(source)
    out = sqlite3.connect(destination)
    try:
        # Consistent against a sampler that is writing underneath us.
        live.backup(out)
        # Copied databases are read far more than written; leaving the WAL
        # behind in the copy means the backup is one file, not three.
        out.execute("PRAGMA journal_mode=DELETE")
    finally:
        out.close()
        live.close()
    return destination


def restore(source, destination=None, keep_previous=True):
    """Replace the live database with `source`.

    The database being replaced is itself backed up first unless asked
    otherwise: a restore is the one operation here that destroys history, and
    restoring the wrong file should be survivable.

    Returns (destination, path of the safety copy or None).
    """
    destination = destination or config.DB_PATH
    ok, why = looks_like_procwatch(source)
    if not ok:
        raise RuntimeError("refusing to restore: %s" % why)
    if os.path.abspath(source) == os.path.abspath(destination):
        raise RuntimeError("source and destination are the same file")

    config.ensure_dirs()
    previous = None
    if keep_previous and os.path.exists(destination):
        previous = destination + ".replaced-" + time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(destination, previous)

    # Written through SQLite rather than copied over the top: the sampler may
    # hold the destination open, and replacing the file underneath an open
    # handle leaves that process writing to a file nobody can see. Opening the
    # destination and overwriting its pages leaves every reader consistent.
    incoming = sqlite3.connect("file:%s?mode=ro" % source, uri=True)
    target = sqlite3.connect(destination)
    try:
        incoming.backup(target)
    finally:
        target.close()
        incoming.close()

    # Anything the restored file predates is re-derived rather than assumed.
    conn = db.connect(destination)
    try:
        db.init_schema(conn)
    finally:
        conn.close()
    return destination, previous


def describe(path):
    """A one-line summary of what a database holds, for confirming a restore."""
    ok, why = looks_like_procwatch(path)
    if not ok:
        return why
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        rows = 0
        oldest, newest = None, None
        for tier in config.TIERS:
            try:
                row = conn.execute(
                    "SELECT COUNT(*), MIN(ts), MAX(ts) FROM sample_%s"
                    % tier.name).fetchone()
            except sqlite3.Error:
                continue
            rows += row[0] or 0
            if row[1]:
                oldest = row[1] if oldest is None else min(oldest, row[1])
                newest = row[2] if newest is None else max(newest, row[2])
    finally:
        conn.close()
    size = os.path.getsize(path) / (1024.0 * 1024.0)
    if oldest is None:
        return "%.1f MB, no samples" % size
    return "%.1f MB, %d samples, %s to %s" % (
        size, rows,
        time.strftime("%Y-%m-%d", time.localtime(oldest)),
        time.strftime("%Y-%m-%d", time.localtime(newest)))
