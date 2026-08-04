"""Tunables. The tier table is the only thing here worth thinking about."""
import collections
import os

# The one place the version is written down.
#
# The menu bar bundle used to declare 1.0 in a heredoc in build.sh, and it had
# said 1.0 through four releases. Procwatch therefore could not see its own
# updates -- it reads CFBundleShortVersionString, that string never moved, and
# by its own correct rule an application whose declared version has not changed
# has not updated. The tool that reports on what your applications do after an
# update was blind to exactly one application: itself.
VERSION = "1.5.0"

Tier = collections.namedtuple("Tier", "name seconds retain_seconds")

INTERVAL = 30  # seconds between samples; must equal the launchd StartInterval

DAY = 86400

# Ordered finest to coarsest. Each tier's bucket must divide evenly into the
# next so bucket boundaries line up instead of drifting. retain_seconds=None
# means keep forever -- only legal on the last tier.
TIERS = [
    Tier("raw",     30,        7 * DAY),
    Tier("fine",    300,      30 * DAY),
    Tier("coarse",  3600,    365 * DAY),
    Tier("archive", 21600,   None),
]

TOP_N = 40          # per metric; recorded set is the union of top CPU and top RSS
OTHER = "__other__"  # synthetic identity holding everything outside the top-N
GAP_FACTOR = 2      # a wall-clock jump beyond INTERVAL*GAP_FACTOR is a gap
ROLLUP_BATCH = 200  # max buckets collapsed per tick, so a tick never stalls
MIN_FREE_BYTES = 512 * 1024 * 1024  # prune early below this much free disk

# Must exceed INTERVAL * GAP_FACTOR: state older than the gap threshold is
# already unusable, but state younger than it must survive to the next tick.
STATE_TTL = 300

_DATA = os.path.expanduser("~/.local/share/procwatch")
_STATE = os.path.expanduser("~/.local/state/procwatch")

DB_PATH = os.path.join(_DATA, "procwatch.db")
LOG_PATH = os.path.join(_STATE, "sampler.log")


def ensure_dirs():
    for d in (_DATA, _STATE):
        os.makedirs(d, exist_ok=True)
