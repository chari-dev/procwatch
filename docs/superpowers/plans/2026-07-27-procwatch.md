# procwatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record per-process CPU and memory on this Mac every 30 seconds, keep the history forever at decaying resolution, and let the owner look back at any past minute to see which process was responsible.

**Architecture:** A stateless Python script respawned by launchd every 30 seconds reads `ps`, computes CPU rates from cumulative-time deltas, writes one batch to SQLite, and collapses expired buckets into coarser tiers on the same pass. A separate on-demand HTTP server reads that database and serves a single self-contained HTML dashboard.

**Tech Stack:** Python 3 (macOS system Python), sqlite3, `unittest` — all stdlib. No third-party packages at runtime or test time.

## Global Constraints

- **No third-party dependencies.** Runtime and tests both. `import` only from the stdlib.
- **Python 3.9+** — the macOS system Python. Do not use `match`, `|` type unions in annotations, or `tomllib`.
- **Target platform is macOS only.** `ps` flags used here are BSD; do not add Linux fallbacks.
- **CPU is never read from `ps %cpu`.** It is a decaying average (`man ps`), not integrable. CPU comes from `cputime` deltas only. A test enforces this.
- **Rollup preserves maxima.** Every tier row carries `cpu_avg` and `cpu_max`. Averaging alone is a defect.
- **Averages are sample-weighted**, never averages-of-averages.
- **Charts stack `cpu_avg` only.** Stacking `cpu_max` is incorrect — maxima did not co-occur.
- **CPU and RSS are summed** across PIDs sharing an identity, never averaged.
- Tiers: `raw` 30s/7d, `fine` 5m/30d, `coarse` 1h/1y, `archive` 6h/forever.
- Database: `~/.local/share/procwatch/procwatch.db`. Logs: `~/.local/state/procwatch/sampler.log`.
- Spec: `docs/superpowers/specs/2026-07-27-procwatch-design.md`. It is the authority; this plan implements it.

---

## File Structure

```
procwatch/
  __init__.py          empty
  config.py            paths, tier table, tunables
  db.py                schema DDL, connection, migration
  identity.py          argv → (exe, args_sig); volatile-token masking
  psreader.py          two ps calls, positional parse, header validation
  system.py            load/memory/swap/disk readings
  sampler.py           CPU deltas, top-N selection, __other__, tick entry point
  rollup.py            tier collapse, weighted mean, max preservation, prune
  server.py            HTTP server + JSON API
  static/index.html    dashboard, inline CSS/JS/SVG, no external assets
  launchd.py           plist generation, load/unload
  cli.py               argparse entry point
tests/
  test_identity.py  test_psreader.py  test_sampler.py
  test_rollup.py    test_retention.py test_acceptance.py
```

Responsibilities are split so the two error-prone areas — identity derivation and rollup arithmetic — are each a single file with a single test file and no other concerns mixed in.

---

### Task 1: Skeleton, config, and schema

**Files:**
- Create: `procwatch/__init__.py`, `procwatch/config.py`, `procwatch/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `config.TIERS` (list of `Tier`), `config.DB_PATH`, `config.LOG_PATH`, `config.INTERVAL`; `db.connect(path) -> sqlite3.Connection`, `db.init_schema(conn) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db.py
import sqlite3, unittest
from procwatch import db, config


class TestSchema(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)

    def _tables(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {r[0] for r in rows}

    def test_every_tier_has_a_sample_and_system_table(self):
        names = self._tables()
        for tier in config.TIERS:
            self.assertIn("sample_" + tier.name, names)
            self.assertIn("system_" + tier.name, names)

    def test_supporting_tables_exist(self):
        self.assertLessEqual(
            {"proc", "watchlist", "gap", "sampler_state"}, self._tables())

    def test_identity_is_unique_on_exe_and_args(self):
        self.conn.execute(
            "INSERT INTO proc (exe, args_sig, cmdline_full) VALUES (?,?,?)",
            ("log", "stream --predicate X", "/usr/bin/log stream --predicate X"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO proc (exe, args_sig, cmdline_full) VALUES (?,?,?)",
                ("log", "stream --predicate X", "different full text"))

    def test_init_schema_is_idempotent(self):
        db.init_schema(self.conn)   # must not raise on a populated database
        self.assertIn("proc", self._tables())

    def test_tiers_are_ordered_coarsening_and_divide_evenly(self):
        for finer, coarser in zip(config.TIERS, config.TIERS[1:]):
            self.assertLess(finer.seconds, coarser.seconds)
            self.assertEqual(coarser.seconds % finer.seconds, 0)

    def test_only_the_last_tier_is_retained_forever(self):
        self.assertIsNone(config.TIERS[-1].retain_seconds)
        for tier in config.TIERS[:-1]:
            self.assertIsNotNone(tier.retain_seconds)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python3 -m unittest tests.test_db -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'procwatch'`

- [ ] **Step 3: Write config.py**

```python
# procwatch/config.py
"""Tunables. The tier table is the only thing here worth thinking about."""
import collections
import os

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

_DATA = os.path.expanduser("~/.local/share/procwatch")
_STATE = os.path.expanduser("~/.local/state/procwatch")

DB_PATH = os.path.join(_DATA, "procwatch.db")
LOG_PATH = os.path.join(_STATE, "sampler.log")


def ensure_dirs():
    for d in (_DATA, _STATE):
        os.makedirs(d, exist_ok=True)
```

- [ ] **Step 4: Write db.py**

```python
# procwatch/db.py
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
  PRIMARY KEY (ts, proc_id)
) WITHOUT ROWID;
"""

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
  expected     INTEGER NOT NULL
) WITHOUT ROWID;
"""

_SUPPORT_DDL = """
CREATE TABLE IF NOT EXISTS proc (
  id           INTEGER PRIMARY KEY,
  exe          TEXT NOT NULL,
  args_sig     TEXT NOT NULL,
  cmdline_full TEXT NOT NULL,
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
        for tier in config.TIERS:
            conn.executescript(_SAMPLE_DDL.format(tier=tier.name))
            conn.executescript(_SYSTEM_DDL.format(tier=tier.name))
```

- [ ] **Step 5: Run the tests**

Run: `python3 -m unittest tests.test_db -v`
Expected: PASS, 6 tests

- [ ] **Step 6: Commit**

```bash
git add procwatch/__init__.py procwatch/config.py procwatch/db.py tests/test_db.py
git commit -m "Add the tier table and the schema it implies"
```

---

### Task 2: Identity derivation

The spec's central correctness risk after the rollup. Getting this wrong either merges distinct processes (hiding the culprit) or splits one process into thousands of rows (unbounded `proc` growth).

**Files:**
- Create: `procwatch/identity.py`
- Test: `tests/test_identity.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `identity.derive(comm, command) -> (exe, args_sig)` where both are `str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_identity.py
import unittest
from procwatch.identity import derive

WEBKIT = ("/System/Library/Frameworks/WebKit.framework/Versions/A/XPCServices/"
          "com.apple.WebKit.WebContent.xpc/Contents/MacOS/com.apple.WebKit.WebContent")


class TestIdentity(unittest.TestCase):
    def test_two_log_streams_with_different_predicates_stay_apart(self):
        a = derive("/usr/bin/log", '/usr/bin/log stream --predicate DUPLEX-TRACE')
        b = derive("/usr/bin/log", '/usr/bin/log stream --predicate OTHER-TRACE')
        self.assertEqual(a[0], "log")
        self.assertNotEqual(a, b)

    def test_renderers_differing_only_in_volatile_tokens_merge(self):
        a = derive(WEBKIT, WEBKIT + " --type=renderer --pid=4765")
        b = derive(WEBKIT, WEBKIT + " --type=renderer --pid=3974")
        self.assertEqual(a, b)

    def test_long_bundle_paths_do_not_collapse_distinct_processes(self):
        # The round-1 bug: a 120-char prefix truncation made these identical.
        base = "/Applications/Some Very Long Vendor Name.app/Contents/Frameworks/"
        one = base + "Helper A.app/Contents/MacOS/Helper A"
        two = base + "Helper B.app/Contents/MacOS/Helper B"
        self.assertGreater(len(base), 60)
        self.assertNotEqual(derive(one, one), derive(two, two))

    def test_exe_is_a_basename_not_a_path(self):
        exe, _ = derive(WEBKIT, WEBKIT)
        self.assertEqual(exe, "com.apple.WebKit.WebContent")

    def test_executable_paths_containing_spaces_split_correctly(self):
        comm = "/System/Applications/Utilities/Activity Monitor.app/Contents/MacOS/Activity Monitor"
        exe, args = derive(comm, comm + " -foo bar")
        self.assertEqual(exe, "Activity Monitor")
        self.assertEqual(args, "-foo bar")

    def test_absolute_paths_in_arguments_reduce_to_basenames(self):
        _, args = derive("/bin/sh", "/bin/sh /Users/you/Developer/site/run.sh")
        self.assertEqual(args, "run.sh")

    def test_uuids_and_temp_paths_are_masked(self):
        c = "/usr/bin/tool"
        a = derive(c, c + " --id 550e8400-e29b-41d4-a716-446655440000")
        b = derive(c, c + " --id 6ba7b810-9dad-11d1-80b4-00c04fd430c8")
        self.assertEqual(a, b)

    def test_args_sig_is_capped_after_normalisation_not_before(self):
        c = "/usr/bin/tool"
        long_args = " ".join("--flag%d" % i for i in range(200))
        _, args = derive(c, c + " " + long_args)
        self.assertLessEqual(len(args), 100)
        self.assertTrue(args.startswith("--flagN"))

    def test_a_process_with_no_arguments_has_an_empty_signature(self):
        self.assertEqual(derive("/sbin/launchd", "/sbin/launchd"), ("launchd", ""))

    def test_command_not_starting_with_comm_falls_back_to_no_args(self):
        # ps occasionally reports a command that does not extend comm.
        exe, args = derive("/usr/bin/thing", "(thing)")
        self.assertEqual(exe, "thing")
        self.assertEqual(args, "")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python3 -m unittest tests.test_identity -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'procwatch.identity'`

- [ ] **Step 3: Write identity.py**

```python
# procwatch/identity.py
"""Turn a process's argv into a stable grouping key.

Two failure modes to avoid. Too coarse and `log stream --predicate X` merges
with every other `log`, hiding the culprit. Too fine and each of 27 browser
renderers becomes its own row because their argv differ by a PID.
"""
import os
import re

MAX_ARGS = 100

# Order matters: UUIDs are matched before bare digit runs, otherwise the digit
# rule chews through a UUID's segments and leaves an unstable remainder.
_VOLATILE = [
    (re.compile(r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"), "UUID"),
    (re.compile(r"\b\d{2,}\b"), "N"),
]


def _split(comm, command):
    """Separate argv[0] from the rest.

    `command` is argv joined by spaces, and macOS executable paths contain
    spaces ("Activity Monitor"), so splitting on whitespace corrupts the
    boundary. `comm` is exactly argv[0], so the prefix gives it away.
    """
    if command == comm:
        return comm, []
    if command.startswith(comm + " "):
        return comm, command[len(comm) + 1:].split()
    # ps reports some processes in a form that does not extend comm at all
    # (kernel threads, "(thing)" for zombies). Keep the exe, drop the args.
    return comm, []


def _normalise(arg):
    if arg.startswith("/"):
        arg = os.path.basename(arg) or "/"
    for pattern, replacement in _VOLATILE:
        arg = pattern.sub(replacement, arg)
    return arg


def derive(comm, command):
    """Return (exe, args_sig) -- the interned identity of a process."""
    exe_path, args = _split(comm, command)
    exe = os.path.basename(exe_path) or exe_path
    sig = " ".join(_normalise(a) for a in args)
    return exe, sig[:MAX_ARGS]
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_identity -v`
Expected: PASS, 10 tests

- [ ] **Step 5: Commit**

```bash
git add procwatch/identity.py tests/test_identity.py
git commit -m "Group processes by executable and normalised arguments"
```

---

### Task 3: Reading ps

**Files:**
- Create: `procwatch/psreader.py`
- Test: `tests/test_psreader.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `psreader.Proc` namedtuple `(pid, start_time, cputime_cs, rss_kb, comm, command)`; `psreader.parse_cputime(str) -> int`; `psreader.parse_lstart(str) -> int`; `psreader.read() -> list[Proc]`; `psreader.PsError`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_psreader.py
import unittest
from procwatch import psreader

MAIN = """\
  PID STARTED                          TIME  RSS COMM
    1 Fri Jul 24 22:17:35 2026      0:50.88 2288 /sbin/launchd
  647 Fri Jul 24 22:18:58 2026     40:08.70 121936 /System/Library/PrivateFrameworks/SkyLight.framework/Resources/WindowServer
 5976 Sun Jul 27 14:24:02 2026   2:03:04.50 138144 /Users/you/Desktop/Notes.app/Contents/MacOS/Notes
"""

CMDS = """\
  PID COMMAND
    1 /sbin/launchd
  647 /System/Library/PrivateFrameworks/SkyLight.framework/Resources/WindowServer
 5976 /Users/you/Desktop/Notes.app/Contents/MacOS/Notes --verbose
"""


class TestParsing(unittest.TestCase):
    def test_cputime_minutes_and_seconds(self):
        self.assertEqual(psreader.parse_cputime("0:50.88"), 5088)

    def test_cputime_with_hours(self):
        self.assertEqual(psreader.parse_cputime("2:03:04.50"), (2 * 3600 + 3 * 60 + 4) * 100 + 50)

    def test_lstart_round_trips_to_a_stable_integer(self):
        a = psreader.parse_lstart("Fri Jul 24 22:17:35 2026")
        b = psreader.parse_lstart("Fri Jul 24 22:17:35 2026")
        self.assertEqual(a, b)
        self.assertGreater(a, 0)

    def test_lstart_tolerates_space_padded_days(self):
        self.assertGreater(psreader.parse_lstart("Fri Jul  4 22:17:35 2026"), 0)


class TestCombine(unittest.TestCase):
    def setUp(self):
        self.procs = {p.pid: p for p in psreader.combine(MAIN, CMDS)}

    def test_lstart_five_tokens_do_not_shift_later_columns(self):
        # A whitespace split would land RSS in the TIME column.
        self.assertEqual(self.procs[647].cputime_cs, (40 * 60 + 8) * 100 + 70)
        self.assertEqual(self.procs[647].rss_kb, 121936)

    def test_comm_keeps_its_full_path_including_spaces(self):
        self.assertTrue(self.procs[647].comm.endswith("WindowServer"))

    def test_command_comes_from_the_second_call(self):
        self.assertEqual(
            self.procs[5976].command,
            "/Users/you/Desktop/Notes.app/Contents/MacOS/Notes --verbose")

    def test_a_pid_missing_from_the_command_call_falls_back_to_comm(self):
        procs = {p.pid: p for p in psreader.combine(MAIN, "  PID COMMAND\n    1 /sbin/launchd\n")}
        self.assertEqual(procs[647].command, procs[647].comm)

    def test_unexpected_header_is_rejected(self):
        # ps drops a bad keyword and still emits the rest; trusting the
        # requested columns rather than the returned ones misreads every row.
        bad = MAIN.replace("STARTED", "ELAPSED")
        with self.assertRaises(psreader.PsError):
            list(psreader.combine(bad, CMDS))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python3 -m unittest tests.test_psreader -v`
Expected: FAIL — no module `procwatch.psreader`

- [ ] **Step 3: Write psreader.py**

```python
# procwatch/psreader.py
"""Read process state from ps.

Two invocations rather than one. `comm` and `command` both contain spaces and
both are variable-width, so a single call emitting both has no parseable
boundary between them. Each call therefore puts its variable-width column last.
"""
import calendar
import collections
import subprocess
import time

Proc = collections.namedtuple(
    "Proc", "pid start_time cputime_cs rss_kb comm command")

MAIN_CMD = ["ps", "-Axo", "pid,lstart,time,rss,comm"]
CMDS_CMD = ["ps", "-Axo", "pid,command"]

MAIN_HEADER = ("PID", "STARTED", "TIME", "RSS", "COMM")
CMDS_HEADER = ("PID", "COMMAND")

_LSTART_TOKENS = 5  # "Fri Jul 24 22:17:35 2026"


class PsError(Exception):
    pass


def parse_cputime(text):
    """'0:50.88' or '2:03:04.50' -> centiseconds."""
    parts = text.split(":")
    total = float(parts[-1])
    if len(parts) >= 2:
        total += int(parts[-2]) * 60
    if len(parts) >= 3:
        total += int(parts[-3]) * 3600
    return int(round(total * 100))


def parse_lstart(text):
    """'Fri Jul 24 22:17:35 2026' -> unix seconds, local time."""
    normalised = " ".join(text.split())
    parsed = time.strptime(normalised, "%a %b %d %H:%M:%S %Y")
    return calendar.timegm(parsed) - _utc_offset(parsed)


def _utc_offset(parsed):
    return -(time.altzone if parsed.tm_isdst == 1 else time.timezone)


def _check_header(line, expected):
    got = tuple(line.split())
    if got != expected:
        raise PsError("unexpected ps header %r, wanted %r" % (got, expected))


def combine(main_text, cmds_text):
    """Merge the two ps outputs into Proc records, keyed by pid."""
    main_lines = main_text.splitlines()
    cmds_lines = cmds_text.splitlines()
    if not main_lines or not cmds_lines:
        raise PsError("empty ps output")
    _check_header(main_lines[0], MAIN_HEADER)
    _check_header(cmds_lines[0], CMDS_HEADER)

    commands = {}
    for line in cmds_lines[1:]:
        pid_text, _, rest = line.strip().partition(" ")
        if pid_text.isdigit():
            commands[int(pid_text)] = rest.strip()

    procs = []
    for line in main_lines[1:]:
        tokens = line.split(None, 3 + _LSTART_TOKENS)
        if len(tokens) < 4 + _LSTART_TOKENS or not tokens[0].isdigit():
            continue
        pid = int(tokens[0])
        start = parse_lstart(" ".join(tokens[1:1 + _LSTART_TOKENS]))
        cputime = parse_cputime(tokens[1 + _LSTART_TOKENS])
        rss = int(tokens[2 + _LSTART_TOKENS])
        comm = tokens[3 + _LSTART_TOKENS].strip()
        procs.append(Proc(pid, start, cputime, rss, comm,
                          commands.get(pid, comm)))
    return procs


def read():
    main = subprocess.run(MAIN_CMD, capture_output=True, text=True, timeout=20)
    cmds = subprocess.run(CMDS_CMD, capture_output=True, text=True, timeout=20)
    if main.returncode != 0:
        raise PsError("ps failed: %s" % main.stderr.strip())
    return combine(main.stdout, cmds.stdout)
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_psreader -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Verify against the real machine**

Run: `python3 -c "from procwatch import psreader; p=psreader.read(); print(len(p)); print(p[0])"`
Expected: a process count in the hundreds and one plausible `Proc`. If this raises `PsError`, the header check is doing its job — read the message and fix the column list rather than loosening the check.

- [ ] **Step 6: Commit**

```bash
git add procwatch/psreader.py tests/test_psreader.py
git commit -m "Read ps in two passes so variable-width columns stay parseable"
```

---

### Task 4: System-wide readings

**Files:**
- Create: `procwatch/system.py`
- Test: `tests/test_system.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `system.Readings` namedtuple `(load1, mem_used_kb, mem_comp_kb, swap_used_kb, disk_free_kb)`; `system.read() -> Readings`; `system.parse_vm_stat(text) -> (used_kb, compressed_kb)`; `system.parse_swapusage(text) -> int`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_system.py
import unittest
from procwatch import system

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                    16902.
Pages active:                                 262520.
Pages inactive:                               261465.
Pages speculative:                               803.
Pages wired down:                             177599.
Pages occupied by compressor:                 292893.
"""

SWAP = "vm.swapusage: total = 2048.00M  used = 1243.06M  free = 804.94M  (encrypted)"


class TestSystem(unittest.TestCase):
    def test_vm_stat_uses_the_declared_page_size(self):
        used_kb, comp_kb = system.parse_vm_stat(VM_STAT)
        # active + wired, at 16 KB per page
        self.assertEqual(used_kb, (262520 + 177599) * 16)
        self.assertEqual(comp_kb, 292893 * 16)

    def test_swapusage_reports_used_megabytes_as_kb(self):
        self.assertEqual(system.parse_swapusage(SWAP), int(1243.06 * 1024))

    def test_read_returns_plausible_live_values(self):
        r = system.read()
        self.assertGreaterEqual(r.load1, 0)
        self.assertGreater(r.mem_used_kb, 0)
        self.assertGreater(r.disk_free_kb, 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python3 -m unittest tests.test_system -v`
Expected: FAIL — no module `procwatch.system`

- [ ] **Step 3: Write system.py**

```python
# procwatch/system.py
"""System-wide readings that sit alongside the per-process rows."""
import collections
import os
import re
import subprocess

from . import config

Readings = collections.namedtuple(
    "Readings", "load1 mem_used_kb mem_comp_kb swap_used_kb disk_free_kb")

_PAGE_SIZE = re.compile(r"page size of (\d+) bytes")
_SWAP_USED = re.compile(r"used = ([\d.]+)M")


def _pages(text, label):
    match = re.search(re.escape(label) + r":\s+(\d+)", text)
    return int(match.group(1)) if match else 0


def parse_vm_stat(text):
    page_match = _PAGE_SIZE.search(text)
    page_kb = (int(page_match.group(1)) if page_match else 4096) // 1024
    used = _pages(text, "Pages active") + _pages(text, "Pages wired down")
    compressed = _pages(text, "Pages occupied by compressor")
    return used * page_kb, compressed * page_kb


def parse_swapusage(text):
    match = _SWAP_USED.search(text)
    return int(float(match.group(1)) * 1024) if match else 0


def _run(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=20).stdout


def read():
    load1 = os.getloadavg()[0]
    mem_used_kb, mem_comp_kb = parse_vm_stat(_run(["vm_stat"]))
    swap_used_kb = parse_swapusage(_run(["sysctl", "vm.swapusage"]))
    stat = os.statvfs(os.path.expanduser("~"))
    disk_free_kb = stat.f_bavail * stat.f_frsize // 1024
    return Readings(int(round(load1 * 100)), mem_used_kb, mem_comp_kb,
                    swap_used_kb, disk_free_kb)


def free_bytes():
    stat = os.statvfs(config.DB_PATH if os.path.exists(config.DB_PATH)
                      else os.path.expanduser("~"))
    return stat.f_bavail * stat.f_frsize
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_system -v`
Expected: PASS, 3 tests

- [ ] **Step 5: Commit**

```bash
git add procwatch/system.py tests/test_system.py
git commit -m "Read load, memory, swap, and free disk alongside the process rows"
```

---

### Task 5: CPU deltas and the sample tick

The measurement core. `load1` is stored scaled by 100 (see Task 4) so the whole schema stays integral.

**Files:**
- Create: `procwatch/sampler.py`
- Test: `tests/test_sampler.py`

**Interfaces:**
- Consumes: `config`, `db`, `identity.derive`, `psreader.Proc`, `system.Readings`.
- Produces: `sampler.cpu_percent(prev_cs, cur_cs, dt) -> float or None`; `sampler.aggregate(procs, prev_state, now, dt) -> (dict[(exe,sig)] -> Agg, dict[pid] -> state)`; `sampler.select(aggs) -> (kept, other)`; `sampler.tick(conn, procs, readings, now) -> None`; `sampler.Agg` with fields `cpu, rss, nproc`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sampler.py
import unittest
from procwatch import config, db, sampler
from procwatch.psreader import Proc
from procwatch.system import Readings


def proc(pid, cputime_cs, rss=1000, comm="/usr/bin/thing", command=None, start=1000):
    return Proc(pid, start, cputime_cs, rss, comm, command or comm)


READINGS = Readings(100, 5000, 100, 0, 999999)


class TestCpuPercent(unittest.TestCase):
    def test_one_second_of_cpu_over_one_second_is_one_hundred_percent(self):
        self.assertAlmostEqual(sampler.cpu_percent(0, 100, 1.0), 100.0)

    def test_half_a_second_over_thirty_seconds(self):
        self.assertAlmostEqual(sampler.cpu_percent(0, 50, 30.0), 1.6667, places=3)

    def test_a_negative_delta_is_discarded(self):
        self.assertIsNone(sampler.cpu_percent(500, 100, 30.0))

    def test_a_non_positive_interval_is_discarded(self):
        # A DST or NTP correction; dividing would write a garbage spike.
        self.assertIsNone(sampler.cpu_percent(0, 100, 0.0))
        self.assertIsNone(sampler.cpu_percent(0, 100, -5.0))


class TestAggregate(unittest.TestCase):
    def test_a_first_sighting_has_no_previous_reading_so_no_rate(self):
        aggs, state = sampler.aggregate([proc(1, 500)], {}, now=100, dt=30.0)
        self.assertEqual(aggs, {})
        self.assertIn(1, state)

    def test_a_recycled_pid_does_not_borrow_the_previous_tenant_cpu_clock(self):
        prev = {1: (1000, 50000)}          # (start_time, cputime_cs)
        aggs, _ = sampler.aggregate([proc(1, 10, start=9999)], prev, now=100, dt=30.0)
        self.assertEqual(aggs, {})

    def test_pids_sharing_an_identity_have_cpu_and_rss_summed(self):
        prev = {1: (1000, 0), 2: (1000, 0)}
        procs = [proc(1, 300, rss=1000), proc(2, 600, rss=2000)]
        aggs, _ = sampler.aggregate(procs, prev, now=100, dt=30.0)
        agg = aggs[("thing", "")]
        self.assertEqual(agg.nproc, 2)
        self.assertEqual(agg.rss, 3000)
        self.assertAlmostEqual(agg.cpu, sampler.cpu_percent(0, 900, 30.0))

    def test_distinct_identities_stay_separate(self):
        prev = {1: (1000, 0), 2: (1000, 0)}
        procs = [
            proc(1, 300, comm="/usr/bin/log", command="/usr/bin/log stream --predicate A"),
            proc(2, 300, comm="/usr/bin/log", command="/usr/bin/log stream --predicate B"),
        ]
        aggs, _ = sampler.aggregate(procs, prev, now=100, dt=30.0)
        self.assertEqual(len(aggs), 2)


class TestSelect(unittest.TestCase):
    def _aggs(self, n):
        return {("p%d" % i, ""): sampler.Agg(cpu=float(i), rss=i * 10, nproc=1)
                for i in range(n)}

    def test_everything_outside_the_top_n_lands_in_other(self):
        kept, other = sampler.select(self._aggs(config.TOP_N + 15))
        self.assertLessEqual(len(kept), config.TOP_N * 2)
        self.assertGreater(other.cpu, 0)

    def test_the_kept_set_is_the_union_of_top_cpu_and_top_rss(self):
        aggs = {
            ("hungry", ""): sampler.Agg(cpu=90.0, rss=1, nproc=1),
            ("fat", ""): sampler.Agg(cpu=0.1, rss=10 ** 9, nproc=1),
        }
        kept, _ = sampler.select(aggs)
        self.assertIn(("hungry", ""), kept)
        self.assertIn(("fat", ""), kept)

    def test_nothing_is_lost_between_kept_and_other(self):
        aggs = self._aggs(config.TOP_N * 3)
        kept, other = sampler.select(aggs)
        total = sum(a.cpu for a in aggs.values())
        self.assertAlmostEqual(sum(a.cpu for a in kept.values()) + other.cpu, total, places=6)


class TestTick(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)

    def _rows(self):
        return self.conn.execute(
            "SELECT p.exe, s.cpu_avg, s.rss_avg, s.nproc FROM sample_raw s "
            "JOIN proc p ON p.id = s.proc_id").fetchall()

    def test_the_first_tick_writes_state_but_no_samples(self):
        sampler.tick(self.conn, [proc(1, 500)], READINGS, now=1000)
        self.assertEqual(self._rows(), [])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sampler_state").fetchone()[0], 1)

    def test_the_second_tick_writes_a_sample(self):
        sampler.tick(self.conn, [proc(1, 0)], READINGS, now=1000)
        sampler.tick(self.conn, [proc(1, 300)], READINGS, now=1030)
        rows = self._rows()
        self.assertEqual(len(rows), 2)      # the process plus __other__
        exes = {r[0] for r in rows}
        self.assertIn("thing", exes)
        self.assertIn(config.OTHER, exes)

    def test_a_sample_row_records_avg_equal_to_max_at_raw_resolution(self):
        sampler.tick(self.conn, [proc(1, 0)], READINGS, now=1000)
        sampler.tick(self.conn, [proc(1, 300)], READINGS, now=1030)
        avg, mx, samples = self.conn.execute(
            "SELECT cpu_avg, cpu_max, samples FROM sample_raw s JOIN proc p "
            "ON p.id = s.proc_id WHERE p.exe = 'thing'").fetchone()
        self.assertEqual(avg, mx)
        self.assertEqual(samples, 1)

    def test_a_long_gap_is_recorded_and_its_delta_discarded(self):
        sampler.tick(self.conn, [proc(1, 0)], READINGS, now=1000)
        sampler.tick(self.conn, [proc(1, 999999)], READINGS, now=1000 + 8 * 3600)
        gaps = self.conn.execute("SELECT ts_start, ts_end, reason FROM gap").fetchall()
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0][2], "sleep")
        self.assertEqual(self._rows(), [])

    def test_identities_are_interned_not_duplicated(self):
        for i, cputime in enumerate([0, 300, 600]):
            sampler.tick(self.conn, [proc(1, cputime)], READINGS, now=1000 + i * 30)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM proc WHERE exe = 'thing'").fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python3 -m unittest tests.test_sampler -v`
Expected: FAIL — no module `procwatch.sampler`

- [ ] **Step 3: Write sampler.py**

```python
# procwatch/sampler.py
"""One tick: read, difference, aggregate, select, write.

CPU is a rate computed from cumulative-time deltas. It is never read from
`ps %cpu`, which is a decaying average over an opaque window (`man ps`),
does not reconcile against a system total, and cannot be integrated across
buckets -- which is exactly what the rollup arithmetic does.
"""
import collections

from . import config, identity

Agg = collections.namedtuple("Agg", "cpu rss nproc")


def cpu_percent(prev_cs, cur_cs, dt):
    """Rate over a known interval, or None when the reading is unusable."""
    if dt <= 0:
        return None
    delta_cs = cur_cs - prev_cs
    if delta_cs < 0:
        return None
    return (delta_cs / 100.0) / dt * 100.0


def aggregate(procs, prev_state, now, dt):
    """Fold processes into per-identity totals; return them and the new state.

    prev_state maps pid -> (start_time, cputime_cs) from the previous tick.
    """
    aggs = {}
    state = {}
    for proc in procs:
        state[proc.pid] = (proc.start_time, proc.cputime_cs)
        previous = prev_state.get(proc.pid)
        if previous is None or previous[0] != proc.start_time:
            continue  # unseen, or a recycled pid whose clock is not ours
        cpu = cpu_percent(previous[1], proc.cputime_cs, dt)
        if cpu is None:
            continue
        key = identity.derive(proc.comm, proc.command)
        current = aggs.get(key)
        if current is None:
            aggs[key] = Agg(cpu, proc.rss_kb, 1)
        else:
            aggs[key] = Agg(current.cpu + cpu,
                            current.rss + proc.rss_kb,
                            current.nproc + 1)
    return aggs, state


def select(aggs):
    """Split into the recorded set and a single __other__ remainder."""
    by_cpu = sorted(aggs, key=lambda k: aggs[k].cpu, reverse=True)[:config.TOP_N]
    by_rss = sorted(aggs, key=lambda k: aggs[k].rss, reverse=True)[:config.TOP_N]
    keys = set(by_cpu) | set(by_rss)
    kept = {k: aggs[k] for k in keys}
    rest = [aggs[k] for k in aggs if k not in keys]
    other = Agg(sum(a.cpu for a in rest),
                sum(a.rss for a in rest),
                sum(a.nproc for a in rest))
    return kept, other


def _proc_id(conn, exe, args_sig, cmdline_full):
    row = conn.execute(
        "SELECT id FROM proc WHERE exe = ? AND args_sig = ?",
        (exe, args_sig)).fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        "INSERT INTO proc (exe, args_sig, cmdline_full) VALUES (?,?,?)",
        (exe, args_sig, cmdline_full))
    return cur.lastrowid


def _load_state(conn):
    rows = conn.execute(
        "SELECT pid, start_time, cputime_cs FROM sampler_state").fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def _last_tick(conn):
    row = conn.execute("SELECT MAX(updated_ts) FROM sampler_state").fetchone()
    return row[0]


def _save_state(conn, state, now):
    conn.executemany(
        "INSERT INTO sampler_state (pid, start_time, cputime_cs, updated_ts) "
        "VALUES (?,?,?,?) ON CONFLICT(pid) DO UPDATE SET "
        "start_time=excluded.start_time, cputime_cs=excluded.cputime_cs, "
        "updated_ts=excluded.updated_ts",
        [(pid, s[0], s[1], now) for pid, s in state.items()])
    conn.execute("DELETE FROM sampler_state WHERE updated_ts < ?", (now - 300,))


def tick(conn, procs, readings, now):
    """Record one sample. Safe to call on an empty or a populated database."""
    with conn:
        previous_ts = _last_tick(conn)
        prev_state = _load_state(conn)
        dt = None if previous_ts is None else now - previous_ts

        if dt is not None and dt > config.INTERVAL * config.GAP_FACTOR:
            conn.execute(
                "INSERT OR IGNORE INTO gap (ts_start, ts_end, reason) VALUES (?,?,?)",
                (previous_ts, now, "sleep"))
            prev_state = {}   # the delta across a sleep is meaningless

        aggs, state = ({}, {}) if dt is None else aggregate(procs, prev_state, now, dt)
        if not aggs and dt is not None:
            _, state = aggregate(procs, {}, now, dt or 1.0)
        if dt is None:
            _, state = aggregate(procs, {}, now, 1.0)

        if aggs:
            kept, other = select(aggs)
            rows = []
            for (exe, sig), agg in list(kept.items()) + [((config.OTHER, ""), other)]:
                full = exe if sig == "" else exe + " " + sig
                pid_row = _proc_id(conn, exe, sig, full)
                cpu = int(round(agg.cpu * 10))
                rows.append((now, pid_row, cpu, cpu, now,
                             agg.rss, agg.rss, agg.nproc, 1))
            conn.executemany(
                "INSERT OR REPLACE INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, "
                "cpu_max_ts, rss_avg, rss_max, nproc, samples) "
                "VALUES (?,?,?,?,?,?,?,?,?)", rows)
            conn.execute(
                "INSERT OR REPLACE INTO system_raw (ts, cpu_busy, load1, mem_used_kb, "
                "mem_comp_kb, swap_used_kb, disk_free_kb, samples, expected) "
                "VALUES (?,?,?,?,?,?,?,1,1)",
                (now, int(round(sum(a.cpu for a in aggs.values()) * 10)),
                 readings.load1, readings.mem_used_kb, readings.mem_comp_kb,
                 readings.swap_used_kb, readings.disk_free_kb))

        _save_state(conn, state, now)
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_sampler -v`
Expected: PASS, 14 tests

- [ ] **Step 5: Commit**

```bash
git add procwatch/sampler.py tests/test_sampler.py
git commit -m "Compute CPU rates from cputime deltas and record one tick"
```

---

### Task 6: Rollup

The part that can quietly corrupt a year of history.

**Files:**
- Create: `procwatch/rollup.py`
- Test: `tests/test_rollup.py`

**Interfaces:**
- Consumes: `config.TIERS`, the tier tables from Task 1.
- Produces: `rollup.bucket_start(ts, seconds) -> int`; `rollup.collapse(conn, finer, coarser, now) -> int` (buckets written); `rollup.prune(conn, now) -> None`; `rollup.run(conn, now) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rollup.py
import unittest
from procwatch import config, db, rollup

HOUR = 3600
DAY = 86400


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)
        self.pid = self.conn.execute(
            "INSERT INTO proc (exe, args_sig, cmdline_full) VALUES ('x','','x')"
        ).lastrowid

    def raw(self, ts, cpu, samples=1, cpu_max=None, cpu_max_ts=None):
        self.conn.execute(
            "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, cpu_max_ts, "
            "rss_avg, rss_max, nproc, samples) VALUES (?,?,?,?,?,?,?,1,?)",
            (ts, self.pid, cpu, cpu_max if cpu_max is not None else cpu,
             cpu_max_ts if cpu_max_ts is not None else ts, 100, 100, samples))
        self.conn.commit()

    def fine_rows(self):
        return self.conn.execute(
            "SELECT ts, cpu_avg, cpu_max, cpu_max_ts, samples FROM sample_fine "
            "ORDER BY ts").fetchall()


class TestBuckets(Base):
    def test_bucket_start_floors_to_the_interval(self):
        self.assertEqual(rollup.bucket_start(1000, 300), 900)
        self.assertEqual(rollup.bucket_start(900, 300), 900)


class TestCollapse(Base):
    def test_a_spike_survives_as_the_max(self):
        # Nine quiet samples and one at 61%. The average is 6.1%.
        base = 0
        for i in range(9):
            self.raw(base + i * 30, 10)
        self.raw(base + 9 * 30, 610)
        rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now=30 * DAY)
        rows = self.fine_rows()
        self.assertEqual(max(r[2] for r in rows), 610)

    def test_the_average_is_sample_weighted_not_an_average_of_averages(self):
        # One bucket carrying 9 samples at 10, one carrying 1 sample at 610.
        self.raw(0, 10, samples=9)
        self.raw(30, 610, samples=1)
        rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now=30 * DAY)
        avg = self.fine_rows()[0][1]
        self.assertEqual(avg, (10 * 9 + 610 * 1) // 10)   # 70, not 310
        self.assertNotEqual(avg, (10 + 610) // 2)

    def test_the_minute_of_the_peak_is_carried_forward(self):
        self.raw(0, 10)
        self.raw(30, 610, cpu_max_ts=30)
        self.raw(60, 10)
        rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now=30 * DAY)
        self.assertEqual(self.fine_rows()[0][3], 30)

    def test_samples_accumulate_so_the_next_tier_can_weight_correctly(self):
        self.raw(0, 10, samples=3)
        self.raw(30, 20, samples=4)
        rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now=30 * DAY)
        self.assertEqual(self.fine_rows()[0][4], 7)

    def test_source_rows_are_deleted(self):
        self.raw(0, 10)
        rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now=30 * DAY)
        left = self.conn.execute("SELECT COUNT(*) FROM sample_raw").fetchone()[0]
        self.assertEqual(left, 0)

    def test_rows_inside_the_retention_window_are_untouched(self):
        now = 30 * DAY
        self.raw(now - 60, 10)          # one minute old
        moved = rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now)
        self.assertEqual(moved, 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sample_raw").fetchone()[0], 1)

    def test_collapse_is_idempotent(self):
        self.raw(0, 10)
        self.raw(30, 610)
        rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now=30 * DAY)
        first = self.fine_rows()
        self.raw(0, 10)   # a crash-and-retry replays the same source rows
        self.raw(30, 610)
        rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now=30 * DAY)
        self.assertEqual(self.fine_rows(), first)

    def test_a_batch_is_bounded_so_a_tick_never_stalls(self):
        for i in range(config.ROLLUP_BATCH * 2 + 50):
            self.raw(i * config.TIERS[1].seconds, 10)
        moved = rollup.collapse(self.conn, config.TIERS[0], config.TIERS[1], now=400 * DAY)
        self.assertLessEqual(moved, config.ROLLUP_BATCH)


class TestPrune(Base):
    def test_the_last_tier_is_never_pruned(self):
        self.conn.execute(
            "INSERT INTO sample_archive (ts, proc_id, cpu_avg, cpu_max, cpu_max_ts, "
            "rss_avg, rss_max, nproc, samples) VALUES (0,?,10,10,0,100,100,1,1)",
            (self.pid,))
        self.conn.commit()
        rollup.prune(self.conn, now=10 * 365 * DAY)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sample_archive").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python3 -m unittest tests.test_rollup -v`
Expected: FAIL — no module `procwatch.rollup`

- [ ] **Step 3: Write rollup.py**

```python
# procwatch/rollup.py
"""Collapse expired buckets into the next coarser tier.

Two rules carry the whole design. cpu_max is the max of the source maxima, so
a four-minute spike still reads at its true height a year later. cpu_avg is
weighted by each source row's sample count -- an average of averages is wrong
whenever buckets hold differing numbers of samples, which happens after every
sleep.
"""
from . import config


def bucket_start(ts, seconds):
    return ts - (ts % seconds)


def _tier_pairs():
    return list(zip(config.TIERS, config.TIERS[1:]))


def collapse(conn, finer, coarser, now):
    """Move rows older than finer.retain_seconds up a tier. Returns buckets written."""
    cutoff = now - finer.retain_seconds
    sample_src, sample_dst = "sample_" + finer.name, "sample_" + coarser.name
    system_src, system_dst = "system_" + finer.name, "system_" + coarser.name

    boundaries = [r[0] for r in conn.execute(
        "SELECT DISTINCT ts / ? FROM %s WHERE ts < ? ORDER BY 1 LIMIT ?" % sample_src,
        (coarser.seconds, cutoff, config.ROLLUP_BATCH)).fetchall()]
    if not boundaries:
        return 0

    with conn:
        for index in boundaries:
            low = index * coarser.seconds
            high = low + coarser.seconds
            rows = conn.execute(
                "SELECT proc_id, cpu_avg, cpu_max, cpu_max_ts, rss_avg, rss_max, "
                "nproc, samples FROM %s WHERE ts >= ? AND ts < ?" % sample_src,
                (low, high)).fetchall()

            merged = {}
            for pid, cpu_avg, cpu_max, cpu_max_ts, rss_avg, rss_max, nproc, samples in rows:
                acc = merged.get(pid)
                if acc is None:
                    merged[pid] = [cpu_avg * samples, cpu_max, cpu_max_ts,
                                   rss_avg * samples, rss_max, nproc, samples]
                    continue
                acc[0] += cpu_avg * samples
                if cpu_max > acc[1]:
                    acc[1], acc[2] = cpu_max, cpu_max_ts
                acc[3] += rss_avg * samples
                acc[4] = max(acc[4], rss_max)
                acc[5] = max(acc[5], nproc)
                acc[6] += samples

            conn.executemany(
                "INSERT INTO %s (ts, proc_id, cpu_avg, cpu_max, cpu_max_ts, rss_avg, "
                "rss_max, nproc, samples) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(ts, proc_id) DO UPDATE SET "
                "cpu_avg=excluded.cpu_avg, cpu_max=excluded.cpu_max, "
                "cpu_max_ts=excluded.cpu_max_ts, rss_avg=excluded.rss_avg, "
                "rss_max=excluded.rss_max, nproc=excluded.nproc, "
                "samples=excluded.samples" % sample_dst,
                [(low, pid, acc[0] // acc[6], acc[1], acc[2],
                  acc[3] // acc[6], acc[4], acc[5], acc[6])
                 for pid, acc in merged.items()])

            sys_rows = conn.execute(
                "SELECT cpu_busy, load1, mem_used_kb, mem_comp_kb, swap_used_kb, "
                "disk_free_kb, samples FROM %s WHERE ts >= ? AND ts < ?" % system_src,
                (low, high)).fetchall()
            if sys_rows:
                weight = sum(r[6] for r in sys_rows) or 1
                conn.execute(
                    "INSERT INTO %s (ts, cpu_busy, load1, mem_used_kb, mem_comp_kb, "
                    "swap_used_kb, disk_free_kb, samples, expected) "
                    "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(ts) DO UPDATE SET "
                    "cpu_busy=excluded.cpu_busy, load1=excluded.load1, "
                    "mem_used_kb=excluded.mem_used_kb, mem_comp_kb=excluded.mem_comp_kb, "
                    "swap_used_kb=excluded.swap_used_kb, "
                    "disk_free_kb=excluded.disk_free_kb, samples=excluded.samples, "
                    "expected=excluded.expected" % system_dst,
                    (low,
                     sum(r[0] * r[6] for r in sys_rows) // weight,
                     sum(r[1] * r[6] for r in sys_rows) // weight,
                     sum(r[2] * r[6] for r in sys_rows) // weight,
                     sum(r[3] * r[6] for r in sys_rows) // weight,
                     sum(r[4] * r[6] for r in sys_rows) // weight,
                     min(r[5] for r in sys_rows),
                     weight,
                     coarser.seconds // config.INTERVAL))
                conn.execute("DELETE FROM %s WHERE ts >= ? AND ts < ?" % system_src,
                             (low, high))

            conn.execute("DELETE FROM %s WHERE ts >= ? AND ts < ?" % sample_src,
                         (low, high))
    return len(boundaries)


def prune(conn, now):
    """Drop anything past the last tier's window. The last tier has none."""
    for tier in config.TIERS:
        if tier.retain_seconds is None:
            continue
        cutoff = now - tier.retain_seconds
        with conn:
            conn.execute("DELETE FROM sample_%s WHERE ts < ?" % tier.name, (cutoff,))
            conn.execute("DELETE FROM system_%s WHERE ts < ?" % tier.name, (cutoff,))


def run(conn, now):
    for finer, coarser in _tier_pairs():
        collapse(conn, finer, coarser, now)
    prune(conn, now)
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_rollup -v`
Expected: PASS, 10 tests

- [ ] **Step 5: Commit**

```bash
git add procwatch/rollup.py tests/test_rollup.py
git commit -m "Collapse tiers while preserving maxima and weighting averages"
```

---

### Task 7: Retention bounds over simulated years

Storage that grows without bound is the failure mode that shows up months later, so it gets its own test rather than riding along with Task 6.

**Files:**
- Create: `tests/test_retention.py`
- Modify: `procwatch/rollup.py` — add `disk_guard`

**Interfaces:**
- Consumes: `rollup.run`, `system.free_bytes`.
- Produces: `rollup.disk_guard(conn, now, free_bytes) -> bool` (True when an early prune ran).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_retention.py
import unittest
from procwatch import config, db, rollup

DAY = 86400


class TestRetention(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)
        self.pid = self.conn.execute(
            "INSERT INTO proc (exe, args_sig, cmdline_full) VALUES ('x','','x')"
        ).lastrowid

    def _fill(self, days, step):
        rows = [(ts, self.pid, 100, 100, ts, 50, 50, 1, 1)
                for ts in range(0, days * DAY, step)]
        self.conn.executemany(
            "INSERT OR REPLACE INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, "
            "cpu_max_ts, rss_avg, rss_max, nproc, samples) VALUES (?,?,?,?,?,?,?,?,?)",
            rows)
        self.conn.commit()

    def _counts(self):
        return {t.name: self.conn.execute(
            "SELECT COUNT(*) FROM sample_%s" % t.name).fetchone()[0]
            for t in config.TIERS}

    def test_two_simulated_years_stay_bounded(self):
        self._fill(days=730, step=3600)     # hourly rows across two years
        now = 730 * DAY
        for _ in range(200):                # run rollup to convergence
            rollup.run(self.conn, now)
        counts = self._counts()
        self.assertEqual(counts["raw"], 0)
        self.assertLess(sum(counts.values()), 40000)

    def test_each_tier_holds_only_its_own_window(self):
        self._fill(days=730, step=3600)
        now = 730 * DAY
        for _ in range(200):
            rollup.run(self.conn, now)
        for tier in config.TIERS:
            if tier.retain_seconds is None:
                continue
            oldest = self.conn.execute(
                "SELECT MIN(ts) FROM sample_%s" % tier.name).fetchone()[0]
            if oldest is not None:
                self.assertGreaterEqual(oldest, now - tier.retain_seconds)

    def test_the_archive_tier_still_holds_the_oldest_data(self):
        self._fill(days=730, step=3600)
        now = 730 * DAY
        for _ in range(200):
            rollup.run(self.conn, now)
        oldest = self.conn.execute("SELECT MIN(ts) FROM sample_archive").fetchone()[0]
        self.assertIsNotNone(oldest)
        self.assertLess(oldest, 30 * DAY)

    def test_low_disk_triggers_an_early_prune(self):
        self._fill(days=10, step=3600)
        ran = rollup.disk_guard(self.conn, now=10 * DAY,
                                free_bytes=config.MIN_FREE_BYTES - 1)
        self.assertTrue(ran)

    def test_ample_disk_does_not_prune_early(self):
        self._fill(days=10, step=3600)
        before = self._counts()
        ran = rollup.disk_guard(self.conn, now=10 * DAY,
                                free_bytes=config.MIN_FREE_BYTES * 10)
        self.assertFalse(ran)
        self.assertEqual(self._counts(), before)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python3 -m unittest tests.test_retention -v`
Expected: FAIL — `AttributeError: module 'procwatch.rollup' has no attribute 'disk_guard'`

- [ ] **Step 3: Add disk_guard to rollup.py**

Append to `procwatch/rollup.py`:

```python
def disk_guard(conn, now, free_bytes):
    """Below the floor, shorten every finite window by half and prune.

    This tool exists because background processes quietly consumed a machine.
    It will not become the process that fills a nearly-full disk.
    """
    if free_bytes >= config.MIN_FREE_BYTES:
        return False
    for tier in config.TIERS:
        if tier.retain_seconds is None:
            continue
        cutoff = now - tier.retain_seconds // 2
        with conn:
            conn.execute("DELETE FROM sample_%s WHERE ts < ?" % tier.name, (cutoff,))
            conn.execute("DELETE FROM system_%s WHERE ts < ?" % tier.name, (cutoff,))
    return True
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_retention -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit**

```bash
git add procwatch/rollup.py tests/test_retention.py
git commit -m "Bound storage across simulated years and guard low disk"
```

---

### Task 8: The tick entry point

**Files:**
- Create: `procwatch/main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `main.run_once(now=None) -> int` (exit status); `main.main() -> int`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_main.py
import os
import tempfile
import unittest
from unittest import mock

from procwatch import main, psreader, system

PROCS = [psreader.Proc(1, 1000, 500, 2048, "/usr/bin/thing", "/usr/bin/thing")]
READINGS = system.Readings(100, 5000, 100, 0, 10 ** 9)


class TestRunOnce(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "t.db")
        self.log = os.path.join(self.dir, "t.log")
        patches = [
            mock.patch("procwatch.config.DB_PATH", self.db),
            mock.patch("procwatch.config.LOG_PATH", self.log),
            mock.patch("procwatch.system.read", return_value=READINGS),
            mock.patch("procwatch.system.free_bytes", return_value=10 ** 12),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_a_successful_tick_returns_zero_and_creates_the_database(self):
        with mock.patch("procwatch.psreader.read", return_value=PROCS):
            self.assertEqual(main.run_once(now=1000), 0)
        self.assertTrue(os.path.exists(self.db))

    def test_a_ps_failure_is_logged_and_does_not_raise(self):
        with mock.patch("procwatch.psreader.read",
                        side_effect=psreader.PsError("boom")):
            self.assertEqual(main.run_once(now=1000), 1)
        with open(self.log) as handle:
            self.assertIn("boom", handle.read())

    def test_consecutive_ticks_produce_a_sample(self):
        with mock.patch("procwatch.psreader.read", return_value=PROCS):
            main.run_once(now=1000)
        later = [PROCS[0]._replace(cputime_cs=800)]
        with mock.patch("procwatch.psreader.read", return_value=later):
            main.run_once(now=1030)
        from procwatch import db
        conn = db.connect(self.db)
        count = conn.execute("SELECT COUNT(*) FROM sample_raw").fetchone()[0]
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python3 -m unittest tests.test_main -v`
Expected: FAIL — no module `procwatch.main`

- [ ] **Step 3: Write main.py**

```python
# procwatch/main.py
"""The launchd entry point. One tick, then exit.

Stateless by design: launchd respawns this every INTERVAL seconds, so nothing
survives in memory and sampler_state is the only carrier between ticks. A
resident daemon would hold memory forever, which is the problem this tool
exists to detect.
"""
import sys
import time
import traceback

from . import config, db, psreader, rollup, sampler, system


def _log(message):
    config.ensure_dirs()
    with open(config.LOG_PATH, "a") as handle:
        handle.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), message))


def run_once(now=None):
    now = int(time.time()) if now is None else now
    try:
        config.ensure_dirs()
        procs = psreader.read()
        readings = system.read()
    except Exception as error:          # a bad read must not kill the agent
        _log("sample failed: %s" % error)
        _log(traceback.format_exc().strip())
        return 1

    conn = None
    try:
        conn = db.connect(config.DB_PATH)
        db.init_schema(conn)
        sampler.tick(conn, procs, readings, now)
        rollup.run(conn, now)
        rollup.disk_guard(conn, now, system.free_bytes())
        return 0
    except Exception as error:
        _log("write failed: %s" % error)
        _log(traceback.format_exc().strip())
        return 1
    finally:
        if conn is not None:
            conn.close()


def main():
    return run_once()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_main -v`
Expected: PASS, 3 tests

- [ ] **Step 5: Run against the real machine twice**

```bash
python3 -m procwatch.main && sleep 31 && python3 -m procwatch.main
sqlite3 ~/.local/share/procwatch/procwatch.db \
  "SELECT p.exe, s.cpu_avg/10.0 FROM sample_raw s JOIN proc p ON p.id=s.proc_id ORDER BY s.cpu_avg DESC LIMIT 5"
```

Expected: five real process names with plausible CPU percentages. If every value is zero, the CPU delta is wrong — check `parse_cputime` before proceeding.

- [ ] **Step 6: Commit**

```bash
git add procwatch/main.py tests/test_main.py
git commit -m "Add the launchd tick entry point"
```

---

### Task 9: launchd integration and CLI

**Files:**
- Create: `procwatch/launchd.py`, `procwatch/cli.py`
- Test: `tests/test_launchd.py`

**Interfaces:**
- Consumes: `config`, `db`, `rollup`.
- Produces: `launchd.PLIST_PATH`, `launchd.plist_text(python, module) -> str`, `launchd.install()`, `launchd.uninstall()`; `cli.main(argv) -> int`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_launchd.py
import plistlib
import unittest
from procwatch import config, launchd


class TestPlist(unittest.TestCase):
    def setUp(self):
        self.plist = plistlib.loads(
            launchd.plist_text("/usr/bin/python3", "procwatch.main").encode())

    def test_the_interval_matches_the_configured_sample_rate(self):
        self.assertEqual(self.plist["StartInterval"], config.INTERVAL)

    def test_it_runs_the_module_not_a_shell_string(self):
        self.assertEqual(
            self.plist["ProgramArguments"], ["/usr/bin/python3", "-m", "procwatch.main"])

    def test_it_does_not_keep_the_process_alive_between_ticks(self):
        # A resident daemon is precisely what this tool exists to detect.
        self.assertFalse(self.plist.get("KeepAlive", False))

    def test_the_label_matches_the_plist_filename(self):
        self.assertTrue(launchd.PLIST_PATH.endswith(self.plist["Label"] + ".plist"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python3 -m unittest tests.test_launchd -v`
Expected: FAIL — no module `procwatch.launchd`

- [ ] **Step 3: Write launchd.py**

```python
# procwatch/launchd.py
"""Generate and load the launchd agent."""
import os
import plistlib
import subprocess
import sys

from . import config

LABEL = "dev.procwatch.sampler"
PLIST_PATH = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)


def plist_text(python, module):
    payload = {
        "Label": LABEL,
        "ProgramArguments": [python, "-m", module],
        "StartInterval": config.INTERVAL,
        "RunAtLoad": True,
        "KeepAlive": False,
        "WorkingDirectory": os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "StandardErrorPath": config.LOG_PATH,
    }
    return plistlib.dumps(payload).decode()


def install():
    config.ensure_dirs()
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    with open(PLIST_PATH, "w") as handle:
        handle.write(plist_text(sys.executable, "procwatch.main"))
    subprocess.run(["launchctl", "unload", PLIST_PATH], capture_output=True)
    subprocess.run(["launchctl", "load", PLIST_PATH], check=True)
    return PLIST_PATH


def uninstall():
    subprocess.run(["launchctl", "unload", PLIST_PATH], capture_output=True)
    if os.path.exists(PLIST_PATH):
        os.remove(PLIST_PATH)
```

- [ ] **Step 4: Write cli.py**

```python
# procwatch/cli.py
"""procwatch install | open | watch | status | uninstall"""
import argparse
import os
import time

from . import config, db, launchd


def _status():
    if not os.path.exists(config.DB_PATH):
        print("no database yet; run `procwatch install`")
        return 1
    conn = db.connect(config.DB_PATH)
    size_mb = os.path.getsize(config.DB_PATH) / (1024.0 * 1024.0)
    print("database   %s  (%.1f MB actual)" % (config.DB_PATH, size_mb))
    last = conn.execute("SELECT MAX(updated_ts) FROM sampler_state").fetchone()[0]
    if last:
        print("last tick  %s (%d s ago)"
              % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last)),
                 int(time.time()) - last))
    for tier in config.TIERS:
        row = conn.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM sample_%s" % tier.name).fetchone()
        span = "empty" if not row[1] else "%.1f days" % ((row[2] - row[1]) / 86400.0)
        print("%-9s %8d rows  %s" % (tier.name, row[0], span))
    gaps = conn.execute("SELECT COUNT(*) FROM gap").fetchone()[0]
    print("gaps       %d recorded" % gaps)
    return 0


def _watch(pattern):
    config.ensure_dirs()
    conn = db.connect(config.DB_PATH)
    db.init_schema(conn)
    with conn:
        conn.execute("INSERT OR REPLACE INTO watchlist (pattern, added_ts) VALUES (?,?)",
                     (pattern, int(time.time())))
    print("watching %s" % pattern)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="procwatch")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install")
    sub.add_parser("uninstall")
    sub.add_parser("status")
    opener = sub.add_parser("open")
    opener.add_argument("--port", type=int, default=8787)
    watcher = sub.add_parser("watch")
    watcher.add_argument("pattern")

    args = parser.parse_args(argv)
    if args.command == "install":
        print("loaded %s" % launchd.install())
        return 0
    if args.command == "uninstall":
        launchd.uninstall()
        print("unloaded; database left at %s" % config.DB_PATH)
        return 0
    if args.command == "status":
        return _status()
    if args.command == "watch":
        return _watch(args.pattern)
    if args.command == "open":
        from . import server
        return server.serve(args.port)
    return 1
```

- [ ] **Step 5: Run the tests**

Run: `python3 -m unittest tests.test_launchd -v`
Expected: PASS, 4 tests

- [ ] **Step 6: Commit**

```bash
git add procwatch/launchd.py procwatch/cli.py tests/test_launchd.py
git commit -m "Add the launchd agent and the command line"
```

---

### Task 10: Query layer and HTTP server

**Files:**
- Create: `procwatch/query.py`, `procwatch/server.py`
- Test: `tests/test_query.py`

**Interfaces:**
- Consumes: `config`, `db`.
- Produces: `query.pick_tier(span_seconds) -> Tier`; `query.series(conn, start, end, limit) -> dict`; `query.bucket_detail(conn, tier_name, ts) -> list[dict]`; `server.serve(port) -> int`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_query.py
import unittest
from procwatch import config, db, query

DAY = 86400


class TestPickTier(unittest.TestCase):
    def test_a_short_window_uses_the_finest_tier(self):
        self.assertEqual(query.pick_tier(3600).name, "raw")

    def test_a_year_long_window_uses_a_coarse_tier(self):
        self.assertIn(query.pick_tier(365 * DAY).name, ("coarse", "archive"))

    def test_wider_windows_never_pick_a_finer_tier(self):
        spans = [3600, DAY, 10 * DAY, 100 * DAY, 1000 * DAY]
        seconds = [query.pick_tier(s).seconds for s in spans]
        self.assertEqual(seconds, sorted(seconds))


class TestSeries(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)
        self.a = self.conn.execute(
            "INSERT INTO proc (exe, args_sig, cmdline_full) VALUES ('a','','a')").lastrowid
        self.b = self.conn.execute(
            "INSERT INTO proc (exe, args_sig, cmdline_full) VALUES (?,'',?)",
            (config.OTHER, config.OTHER)).lastrowid
        for ts in range(0, 300, 30):
            self.conn.executemany(
                "INSERT INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, cpu_max_ts, "
                "rss_avg, rss_max, nproc, samples) VALUES (?,?,?,?,?,?,?,?,?)",
                [(ts, self.a, 100, 610, ts, 500, 500, 3, 1),
                 (ts, self.b, 50, 50, ts, 100, 100, 9, 1)])
        self.conn.commit()

    def test_series_returns_both_avg_and_max_per_point(self):
        result = query.series(self.conn, 0, 300, limit=10)
        point = result["series"][0]["points"][0]
        self.assertIn("cpu_avg", point)
        self.assertIn("cpu_max", point)
        self.assertNotEqual(point["cpu_avg"], point["cpu_max"])

    def test_percentages_are_returned_unscaled(self):
        result = query.series(self.conn, 0, 300, limit=10)
        by_name = {s["exe"]: s for s in result["series"]}
        self.assertAlmostEqual(by_name["a"]["points"][0]["cpu_max"], 61.0)

    def test_the_other_row_is_present_and_marked(self):
        result = query.series(self.conn, 0, 300, limit=10)
        other = [s for s in result["series"] if s["is_other"]]
        self.assertEqual(len(other), 1)

    def test_bucket_detail_is_ranked_and_carries_the_process_count(self):
        rows = query.bucket_detail(self.conn, "raw", 60)
        self.assertEqual(rows[0]["exe"], "a")
        self.assertEqual(rows[0]["nproc"], 3)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python3 -m unittest tests.test_query -v`
Expected: FAIL — no module `procwatch.query`

- [ ] **Step 3: Write query.py**

```python
# procwatch/query.py
"""Read side. Picks a tier for the requested span and shapes JSON."""
from . import config

# A window wider than this many buckets is too dense to render usefully.
TARGET_BUCKETS = 400


def pick_tier(span_seconds):
    for tier in config.TIERS:
        if span_seconds / float(tier.seconds) <= TARGET_BUCKETS:
            return tier
    return config.TIERS[-1]


def series(conn, start, end, limit=12):
    tier = pick_tier(max(end - start, 1))
    table = "sample_" + tier.name

    ranked = conn.execute(
        "SELECT proc_id, MAX(cpu_max) FROM %s WHERE ts >= ? AND ts < ? "
        "GROUP BY proc_id ORDER BY 2 DESC LIMIT ?" % table,
        (start, end, limit)).fetchall()
    ids = [r[0] for r in ranked]
    if not ids:
        return {"tier": tier.name, "series": [], "gaps": [], "system": []}

    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT s.proc_id, p.exe, p.cmdline_full, s.ts, s.cpu_avg, s.cpu_max, "
        "s.cpu_max_ts, s.rss_avg, s.rss_max, s.nproc FROM %s s "
        "JOIN proc p ON p.id = s.proc_id "
        "WHERE s.ts >= ? AND s.ts < ? AND s.proc_id IN (%s) ORDER BY s.ts"
        % (table, placeholders), [start, end] + ids).fetchall()

    grouped = {}
    for pid, exe, full, ts, cpu_avg, cpu_max, cpu_max_ts, rss_avg, rss_max, nproc in rows:
        entry = grouped.setdefault(pid, {
            "exe": exe, "cmdline": full, "is_other": exe == config.OTHER, "points": []})
        entry["points"].append({
            "ts": ts, "cpu_avg": cpu_avg / 10.0, "cpu_max": cpu_max / 10.0,
            "cpu_max_ts": cpu_max_ts, "rss_avg": rss_avg, "rss_max": rss_max,
            "nproc": nproc})

    system = [
        {"ts": r[0], "cpu_busy": r[1] / 10.0, "load1": r[2] / 100.0,
         "mem_used_kb": r[3], "swap_used_kb": r[4],
         "coverage": (r[5] / float(r[6])) if r[6] else 1.0}
        for r in conn.execute(
            "SELECT ts, cpu_busy, load1, mem_used_kb, swap_used_kb, samples, expected "
            "FROM system_%s WHERE ts >= ? AND ts < ? ORDER BY ts" % tier.name,
            (start, end)).fetchall()]

    gaps = [{"start": r[0], "end": r[1], "reason": r[2]} for r in conn.execute(
        "SELECT ts_start, ts_end, reason FROM gap WHERE ts_end >= ? AND ts_start < ?",
        (start, end)).fetchall()]

    return {"tier": tier.name, "series": [grouped[i] for i in ids if i in grouped],
            "system": system, "gaps": gaps}


def bucket_detail(conn, tier_name, ts):
    rows = conn.execute(
        "SELECT p.exe, p.cmdline_full, s.cpu_avg, s.cpu_max, s.rss_avg, s.nproc "
        "FROM sample_%s s JOIN proc p ON p.id = s.proc_id "
        "WHERE s.ts = ? ORDER BY s.cpu_avg DESC" % tier_name, (ts,)).fetchall()
    return [{"exe": r[0], "cmdline": r[1], "cpu_avg": r[2] / 10.0,
             "cpu_max": r[3] / 10.0, "rss_kb": r[4], "nproc": r[5]} for r in rows]
```

- [ ] **Step 4: Write server.py**

```python
# procwatch/server.py
"""On-demand local dashboard server. Exits when idle."""
import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config, db, query

IDLE_TIMEOUT = 900
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, content_type):
        payload = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.server.last_seen = time.time()
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        conn = db.connect(config.DB_PATH)
        try:
            if parsed.path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as handle:
                    return self._send(200, handle.read(), "text/html; charset=utf-8")
            if parsed.path == "/api/series":
                end = int(params.get("end", [int(time.time())])[0])
                start = int(params.get("start", [end - 86400])[0])
                return self._send(200, json.dumps(query.series(conn, start, end)),
                                  "application/json")
            if parsed.path == "/api/bucket":
                return self._send(200, json.dumps(query.bucket_detail(
                    conn, params["tier"][0], int(params["ts"][0]))),
                    "application/json")
            self._send(404, "not found", "text/plain")
        finally:
            conn.close()


def serve(port):
    if not os.path.exists(config.DB_PATH):
        print("no database yet; run `procwatch install` and wait a minute")
        return 1
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.last_seen = time.time()

    def reap():
        while time.time() - httpd.last_seen < IDLE_TIMEOUT:
            time.sleep(30)
        httpd.shutdown()

    threading.Thread(target=reap, daemon=True).start()
    url = "http://127.0.0.1:%d/" % port
    print("serving %s  (exits after %d minutes idle)" % (url, IDLE_TIMEOUT // 60))
    webbrowser.open(url)
    httpd.serve_forever()
    return 0
```

- [ ] **Step 5: Run the tests**

Run: `python3 -m unittest tests.test_query -v`
Expected: PASS, 7 tests

- [ ] **Step 6: Commit**

```bash
git add procwatch/query.py procwatch/server.py tests/test_query.py
git commit -m "Serve the history over a local JSON API"
```

---

### Task 11: Dashboard

**Files:**
- Create: `procwatch/static/index.html`
- Test: manual, plus `tests/test_static.py` for the invariants that can be checked without a browser

**Interfaces:**
- Consumes: `/api/series`, `/api/bucket`.
- Produces: nothing other tasks depend on.

**Visual target: Beszel.** A screenshot of Beszel's system page is at
`.superpowers/sdd/2026-07-27-procwatch/beszel-reference.png`. **Open it and match it.**
It is the agreed look, not an inspiration board. What to copy:

- **Shell.** Near-black page (`#09090b`), cards one step lighter (`#111113`) with a 1px
  border (`#27272a`) and ~12px radius. Generous padding. Sans stack, tight leading.
- **Header bar.** Wordmark left. Right side: a theme toggle and the time-range control.
  No search, no user menu, no "Add System" — this tool watches one machine.
- **Title block.** Machine name large and bold, and beneath it a single horizontal strip
  of small muted items, each with a leading glyph, separated by thin dividers: a green
  status dot with "Recording", the hostname, "macOS 27.0", "Apple M3 (8 cores)", "16 GB",
  uptime, and time since last tick. Mirror the reference's `Up · host · os · 602 days ·
  kernel · CPU` strip.
- **Time-range pill.** Top-right, clock glyph + label + chevron: 1 hour, 12 hours,
  24 hours, 1 week, 30 days, 1 year. This is the only tier control — the server picks the
  tier from the span, so these labels drive `/api/series` bounds and nothing else.
- **Two-column card grid**, one chart per card, collapsing to one column under ~900px.
- **Card interior.** Bold title, then a muted one-line description beneath it — the
  reference's "Average system-wide CPU utilization" pattern. Then the chart.
- **Charts.** Filled area with a brighter stroke on top. Y labels left, muted, with units
  (`40%`, `2 GB`, `0.06 MB/s`). X labels are times (`4:00 PM`). Faint horizontal
  gridlines only — no plot border, no axis spines.
- **Tooltip.** This is the drill-down. Dark panel, timestamp header (`Jul 27, 4:05 PM`),
  then one row per process: colored swatch, name, right-aligned value, sorted descending.
  Exactly the reference's Docker CPU tooltip.
- **Legend.** Small swatch + label row beneath each stacked chart.
- **Per-metric colors**, following the reference: system CPU blue, memory teal/green,
  swap purple, disk amber. Stacked per-process bands use a full spectrum ramp the way
  the reference's container charts do.

Cards to build: **CPU by Process** (stacked), **Memory by Process** (stacked),
**System CPU**, **Load Average**, **Swap Used**, **Free Disk**.

- [ ] **Step 1: Read the skills, then the screenshot**

Load `frontend-design` and `dataviz` before writing any markup. Then open
`.superpowers/sdd/2026-07-27-procwatch/beszel-reference.png` with the Read tool and keep
it open while you work — matching it is the acceptance criterion for this task.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_static.py
import os
import re
import unittest

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "procwatch", "static", "index.html")


class TestDashboardSource(unittest.TestCase):
    def setUp(self):
        with open(PATH) as handle:
            self.html = handle.read()

    def test_it_loads_nothing_from_the_network(self):
        # A local monitor must work with the machine offline.
        for pattern in (r'src=["\']https?:', r'href=["\']https?://',
                        r'@import\s+url\(https?:'):
            self.assertIsNone(re.search(pattern, self.html), pattern)

    def test_the_stacked_area_is_built_from_avg_not_max(self):
        # Stacking maxima sums peaks that never co-occurred.
        self.assertIn("cpu_avg", self.html)
        stack = re.search(r"function stack[\s\S]{0,600}", self.html)
        self.assertIsNotNone(stack)
        self.assertNotIn("cpu_max", stack.group(0))

    def test_it_renders_both_light_and_dark(self):
        self.assertIn("prefers-color-scheme", self.html)

    def test_gaps_are_drawn_rather_than_interpolated(self):
        self.assertIn("coverage", self.html)

    def test_every_beszel_time_range_is_offered(self):
        for label in ("1 hour", "12 hours", "24 hours", "1 week", "30 days", "1 year"):
            self.assertIn(label, self.html)

    def test_every_planned_card_is_present(self):
        for title in ("CPU by Process", "Memory by Process", "System CPU",
                      "Load Average", "Swap Used", "Free Disk"):
            self.assertIn(title, self.html)

    def test_the_tooltip_sorts_descending_like_the_reference(self):
        self.assertRegex(self.html, r"sort\([^)]*\)[\s\S]{0,120}(b\.|-\s*a\.)")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run it to confirm it fails**

Run: `python3 -m unittest tests.test_static -v`
Expected: FAIL — `FileNotFoundError`

- [ ] **Step 4: Write the dashboard**

Build `procwatch/static/index.html` as one self-contained file — inline `<style>`, inline `<script>`, hand-built inline SVG. No frameworks, no CDN, no fetch to any host but the local origin. It must contain:

- A **stacked area chart** of CPU over the window, one band per identity, stacked from `cpu_avg`. A `stack()` function that touches only `cpu_avg` — the test asserts this, because stacking `cpu_max` sums peaks that never co-occurred and can exceed 100% × cores.
- A **peak marker** per series at its `cpu_max`, positioned at `cpu_max_ts`, with the value in its title attribute. This is how a rolled-up spike stays findable.
- **Gap rendering**: for any `system` point with `coverage < 0.5`, draw a hatched vertical band and break the area path there. Never draw a line segment across a gap.
- A **time brush**: drag to select a range, which refetches `/api/series` with new bounds. The server picks the tier, so zooming into last night moves from `coarse` to `raw` with no client logic.
- **Click a bucket** → fetch `/api/bucket` and render the ranked table below the chart: exe, full command line, avg, max, RSS, `nproc`.
- A **memory chart** on the same time axis, and a system strip showing load and swap.
- Light and dark via `prefers-color-scheme`.

- [ ] **Step 5: Run the tests**

Run: `python3 -m unittest tests.test_static -v`
Expected: PASS, 7 tests

- [ ] **Step 6: Look at it**

```bash
python3 -m procwatch.cli open
```

Expected: a browser tab with real data. Confirm by eye that the stacked bands do not exceed 100% × 8 cores, that peak markers appear, and that the drill-down table matches the chart.

- [ ] **Step 7: Commit**

```bash
git add procwatch/static/index.html tests/test_static.py
git commit -m "Add the dashboard"
```

---

### Task 12: Acceptance

The tests that justify the whole design. If these pass, the tool does its job.

**Files:**
- Create: `tests/test_acceptance.py`
- Create: `README.md`

**Interfaces:**
- Consumes: everything.
- Produces: nothing.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_acceptance.py
"""The two claims the design rests on."""
import unittest

from procwatch import config, db, rollup, sampler, system
from procwatch.psreader import Proc

DAY = 86400
READINGS = system.Readings(100, 5000, 100, 0, 10 ** 12)


class TestSpikeSurvives(unittest.TestCase):
    """A 61% four-minute spike must still read 61% in the archive tier."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)

    def test_a_spike_is_still_findable_a_year_later(self):
        # Why cpu_max exists: across these two hours the four-minute 61%
        # burst averages to (61.0*8 + 1.0*232) / 240 == 3.0%, so a rollup
        # that kept only the average would show nothing at all here.
        now = 0
        cputime = 0
        # Two hours of near-idle with a four-minute 61% burst in the middle.
        for step in range(240):
            busy = 61.0 if 100 <= step < 108 else 1.0
            cputime += int(busy * config.INTERVAL)   # centiseconds
            sampler.tick(
                self.conn,
                [Proc(1, 1, cputime, 1024, "/usr/bin/hog", "/usr/bin/hog")],
                READINGS, now)
            now += config.INTERVAL

        later = now + 400 * DAY
        for _ in range(500):
            rollup.run(self.conn, later)

        peak, when = self.conn.execute(
            "SELECT MAX(cpu_max), cpu_max_ts FROM sample_archive s "
            "JOIN proc p ON p.id = s.proc_id WHERE p.exe = 'hog'").fetchone()
        self.assertAlmostEqual(peak / 10.0, 61.0, delta=1.0)
        # And it still names the minute.
        self.assertGreaterEqual(when, 100 * config.INTERVAL)
        self.assertLess(when, 108 * config.INTERVAL)


class TestAdditivity(unittest.TestCase):
    """Per-process rows plus __other__ must reconcile against the system total.

    This is the test that rules out ps %cpu, whose fields are documented to
    sum past 100%.
    """

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)

    def _procs(self, count, cputime):
        return [Proc(i, 1, cputime, 1024, "/usr/bin/p%d" % i, "/usr/bin/p%d" % i)
                for i in range(count)]

    def test_the_kept_rows_plus_other_equal_the_system_busy_total(self):
        count = config.TOP_N * 3       # forces a populated __other__
        sampler.tick(self.conn, self._procs(count, 0), READINGS, 0)
        sampler.tick(self.conn, self._procs(count, 300), READINGS, config.INTERVAL)

        total = self.conn.execute(
            "SELECT SUM(cpu_avg) FROM sample_raw WHERE ts = ?",
            (config.INTERVAL,)).fetchone()[0]
        busy = self.conn.execute(
            "SELECT cpu_busy FROM system_raw WHERE ts = ?",
            (config.INTERVAL,)).fetchone()[0]
        self.assertAlmostEqual(total, busy, delta=count)   # integer rounding only

    def test_other_is_not_empty_when_processes_exceed_the_top_n(self):
        count = config.TOP_N * 3
        sampler.tick(self.conn, self._procs(count, 0), READINGS, 0)
        sampler.tick(self.conn, self._procs(count, 300), READINGS, config.INTERVAL)
        other = self.conn.execute(
            "SELECT s.nproc FROM sample_raw s JOIN proc p ON p.id = s.proc_id "
            "WHERE p.exe = ?", (config.OTHER,)).fetchone()
        self.assertIsNotNone(other)
        self.assertGreater(other[0], 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it**

Run: `python3 -m unittest tests.test_acceptance -v`
Expected: initially FAIL if any earlier task cut a corner. These are the gates — fix the implementation, never the assertion.

- [ ] **Step 3: Run the whole suite**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS, all tests across all files.

- [ ] **Step 4: Write README.md**

```markdown
# procwatch

Per-process CPU and memory history for macOS. Records every 30 seconds,
keeps it forever at decaying resolution, and lets you look back at any
past minute to see which process was responsible.

    python3 -m procwatch.cli install    # load the launchd agent
    python3 -m procwatch.cli open       # dashboard in a browser
    python3 -m procwatch.cli status     # rows, windows, actual size on disk

Storage settles around 75 MB and grows ~3 MB a year. Design and rationale:
`docs/superpowers/specs/2026-07-27-procwatch-design.md`.

Two things worth knowing if you change the code:

- CPU is computed from `cputime` deltas, never read from `ps %cpu` — that
  column is a decaying average over an opaque window and cannot be summed
  or integrated. `tests/test_acceptance.py` enforces this.
- Rolled-up rows keep a max alongside the average. Averaging alone turns a
  four-minute 61% spike into 3%, which is the whole thing this exists to catch.
```

- [ ] **Step 5: Install it and watch it run**

```bash
python3 -m procwatch.cli install
sleep 90
python3 -m procwatch.cli status
```

Expected: `status` reports a recent tick and a growing raw tier.

- [ ] **Step 6: Commit**

```bash
git add tests/test_acceptance.py README.md
git commit -m "Pin the two claims the design rests on"
```

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: architecture → 1/8/10; data source → 3; CPU deltas → 5; identity → 2; schema → 1; tiers and storage → 1/6/7; rollup arithmetic → 6; sampler lifecycle → 8/9; gaps → 5/10/11; dashboard → 11; CLI → 9; error handling → 8; testing → every task, gated by 12.

**Deliberately deferred.** The watchlist has a table and a CLI verb (Task 9) but the sampler does not yet consult it — the top-N union covers every case the spec motivates, and wiring it in without a use case would be speculative. `procwatch status` reports actual size, which the spec requires precisely because the estimate is best-case.

**Known rough edge.** `sampler.tick` handles the first-tick and post-gap state paths with more branching than the logic warrants. It is correct and tested; if Task 5's implementer sees a cleaner factoring, take it.
