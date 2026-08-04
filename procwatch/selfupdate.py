"""Check GitHub for a newer procwatch, and install it.

The version lives in one place, config.VERSION, so "is there a newer one"
is answered by reading that same constant off the repository's main branch
-- no tags, no release machinery, one HTTPS GET. The install has two shapes
and each updates the way it was installed: a git checkout pulls, and the
single-file bundle downloads a fresh copy of itself and swaps it in place.

What this never does is restart anything. The server that ran apply() is
still executing the old code, and pretending otherwise -- exec'ing over a
threaded HTTP server mid-request -- is how an update turns into a hang. The
answer carries restart=True and the page says "quit and reopen".
"""
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request

from . import config

REPO = "chari-dev/procwatch"
BRANCH = "main"
RAW = "https://raw.githubusercontent.com/%s/%s/" % (REPO, BRANCH)
REPO_URL = "https://github.com/" + REPO

# How long a verdict is trusted before the repository is asked again. The
# server lives at most a browsing session, so this is "once per session"
# in practice, with force= for the button that says check *now*.
CHECK_TTL = 6 * 3600

_VERSION_RE = re.compile(r'^VERSION\s*=\s*"([0-9][0-9.]*)"', re.MULTILINE)

# The marker tools/bundle.py stamps into every inlined module's __file__.
_BUNDLE_MARK = "<procwatch bundle>"

_CACHE = {"ts": 0.0, "answer": None}


def mode():
    """How this install can be updated: "bundle", "git", or "none".

    The bundle marker is checked first: inside the single file, this module's
    __file__ is the marker string, and asking os.path questions about it
    would answer for a directory that does not exist.
    """
    if __file__.startswith(_BUNDLE_MARK):
        return "bundle"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.isdir(os.path.join(root, ".git")):
        return "git"
    return "none"


def _checkout_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fetch(url, limit=8 * 1024 * 1024):
    """One HTTPS GET, as text. Raises OSError on anything that went wrong."""
    request = urllib.request.Request(
        url, headers={"User-Agent": "procwatch/" + config.VERSION})
    with urllib.request.urlopen(request, timeout=15) as reply:
        return reply.read(limit).decode("utf-8")


def _parse_version(text):
    """The VERSION constant out of config.py's source, or None."""
    found = _VERSION_RE.search(text)
    return found.group(1) if found else None


def _numbers(version):
    """"1.10.2" -> (1, 10, 2), so 1.10 sorts after 1.9 rather than before."""
    return tuple(int(part) for part in version.split(".") if part != "")


def _newer(latest, current):
    try:
        return _numbers(latest) > _numbers(current)
    except (ValueError, AttributeError):
        return False


def check(force=False, now=None):
    """Whether a newer version exists on main. Never raises.

    The answer is cached for CHECK_TTL so a dashboard that polls does not
    turn into a poll of GitHub; force asks fresh regardless.
    """
    now = time.time() if now is None else now
    cached = _CACHE["answer"]
    if cached is not None and not force and now - _CACHE["ts"] < CHECK_TTL:
        return cached
    answer = {"current": config.VERSION, "latest": None, "newer": False,
              "mode": mode(), "error": "", "checked_ts": int(now)}
    try:
        latest = _parse_version(_fetch(RAW + "procwatch/config.py"))
        if latest is None:
            answer["error"] = "could not read the version from the repository"
        else:
            answer["latest"] = latest
            answer["newer"] = _newer(latest, config.VERSION)
    except OSError as problem:
        answer["error"] = "could not reach GitHub: %s" % problem
    # Failures are cached too: a machine that is offline should not retry
    # on every poll, and force= exists for trying again on purpose.
    _CACHE.update({"ts": now, "answer": answer})
    return answer


def _bundle_target():
    """The single file this process was started from.

    __main__ is procwatch.py itself in every way the bundle runs -- the CLI,
    `procwatch open`, the menu bar server -- because the bundle *is* the
    program. Refuses rather than guesses when that does not hold.
    """
    main = sys.modules.get("__main__")
    path = getattr(main, "__file__", None)
    if not path or not os.path.isfile(path):
        raise RuntimeError("cannot find the running procwatch.py to replace")
    return os.path.realpath(path)


def _apply_bundle():
    target = _bundle_target()
    text = _fetch(RAW + "procwatch.py")
    # Refusals before the write, because the write is the point of no return:
    # the file must be a plausible procwatch bundle, it must at least parse,
    # and it must actually be newer -- replacing a working copy with the same
    # or an older one only manufactures confusion.
    got = _parse_version(text)
    if not text.startswith("#!") or "procwatch" not in text[:400] or not got:
        raise RuntimeError("the download does not look like procwatch.py")
    if not _newer(got, config.VERSION):
        raise RuntimeError("the repository has %s, which is not newer than %s"
                           % (got, config.VERSION))
    compile(text, "procwatch.py", "exec")  # SyntaxError -> refused below
    staging = target + ".new"
    # utf-8 by name, not by locale: launchd starts things with whatever
    # locale it has, and the file must land byte-for-byte as fetched.
    with open(staging, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(staging, 0o755)
    os.replace(staging, target)
    return got


def _apply_git():
    root = _checkout_root()
    for step in (["git", "-C", root, "fetch", "origin", BRANCH],
                 ["git", "-C", root, "merge", "--ff-only",
                  "origin/" + BRANCH]):
        done = subprocess.run(step, capture_output=True, text=True,
                              timeout=120)
        if done.returncode != 0:
            # git's own message names the actual problem -- diverged, dirty,
            # offline -- better than any summary of it would.
            raise RuntimeError((done.stderr or done.stdout).strip()
                               or "git failed")
    return _parse_version(
        open(os.path.join(root, "procwatch", "config.py")).read())


# Which versions have run against this database, and when each first did.
# What turns an update into something the timeline can show: apply() swaps
# the code, but the update has not *happened* until the new version runs,
# and this table is how the first run of it gets noticed -- whichever way
# the code arrived, this updater, a git pull, or a fresh download.
_SEEN_DDL = """
CREATE TABLE IF NOT EXISTS self_version (
  version  TEXT PRIMARY KEY,
  first_ts INTEGER NOT NULL
);
"""


def _inherited_prior(conn):
    """The version that ran here before this table existed, if anything knows.

    versions.py has been recording the installed Procwatch.app's declared
    version all along, so a database older than self_version is not actually
    ignorant of its past -- and without this, every install predating the
    table would have its first real update pass unannounced.
    """
    try:
        row = conn.execute(
            "SELECT version, first_ts FROM app_version WHERE app = ? "
            "ORDER BY first_ts DESC LIMIT 1", ("Procwatch",)).fetchone()
    except sqlite3.Error:
        return None
    return row if row else None


def note_if_updated(conn, now=None):
    """Record that this version is running; say whether that is news.

    Returns {"from", "to", "ts"} the first time a new version runs after a
    previous one, and None otherwise -- on every later tick, and on the very
    first install, which is a beginning rather than an update.
    """
    now = int(time.time()) if now is None else now
    with conn:
        conn.executescript(_SEEN_DDL)
    row = conn.execute("SELECT version FROM self_version "
                       "ORDER BY first_ts DESC, version DESC LIMIT 1"
                       ).fetchone()
    prior = row[0] if row else None
    if prior is None:
        inherited = _inherited_prior(conn)
        if inherited is not None and inherited[0] != config.VERSION:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO self_version (version, first_ts) "
                    "VALUES (?,?)", (inherited[0], int(inherited[1])))
            prior = inherited[0]
    if prior == config.VERSION:
        return None
    with conn:
        changed = conn.execute(
            "INSERT OR IGNORE INTO self_version (version, first_ts) "
            "VALUES (?,?)", (config.VERSION, now)).rowcount
    # A version already in the table is a downgrade to something that has
    # run here before; recording it again would rewrite when it first ran.
    if prior is None or not changed:
        return None
    return {"from": prior, "to": config.VERSION, "ts": now}


def apply():
    """Install the newer version, the way this copy was installed.

    Returns what happened rather than raising: the caller is an HTTP handler
    and a button, and both want a sentence, not a traceback.
    """
    how = mode()
    answer = {"ok": False, "mode": how, "from": config.VERSION, "to": None,
              "restart": False, "error": ""}
    try:
        if how == "bundle":
            answer["to"] = _apply_bundle()
        elif how == "git":
            answer["to"] = _apply_git()
        else:
            answer["error"] = ("this install is neither a git checkout nor "
                               "the single-file bundle; update it the way it "
                               "was installed")
            return answer
        answer["ok"] = True
        answer["restart"] = True
        # The next check() must not keep advertising the version that was
        # just installed against code that has not restarted yet.
        _CACHE.update({"ts": 0.0, "answer": None})
    except (OSError, RuntimeError, SyntaxError,
            subprocess.TimeoutExpired) as problem:
        answer["error"] = str(problem)
    return answer
