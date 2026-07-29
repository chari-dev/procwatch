# procwatch/psreader.py
"""Read process state from ps.

Two invocations rather than one. `comm` and `command` both contain spaces and
both are variable-width, so a single call emitting both has no parseable
boundary between them. Each call therefore puts its variable-width column last.
"""
import collections
import subprocess
import time

Proc = collections.namedtuple(
    "Proc", "pid start_time cputime_cs rss_kb comm command ppid",
    defaults=(0,))

MAIN_CMD = ["ps", "-Axo", "pid,lstart,time,rss,comm"]
PPID_CMD = ["ps", "-Axo", "pid,ppid"]
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
    # mktime resolves tm_isdst=-1 through libc, which knows whether this
    # particular date was inside DST. Computing the offset by hand cannot:
    # strptime never sets tm_isdst from a format without %Z.
    return int(time.mktime(parsed))


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


def parents(text):
    """{pid: ppid} from `ps -Axo pid,ppid`."""
    out = {}
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            out[int(parts[0])] = int(parts[1])
    return out


def read():
    main = subprocess.run(MAIN_CMD, capture_output=True, text=True, timeout=20)
    cmds = subprocess.run(CMDS_CMD, capture_output=True, text=True, timeout=20)
    if main.returncode != 0:
        raise PsError("ps failed: %s" % main.stderr.strip())
    if cmds.returncode != 0:
        raise PsError("ps (command) failed: %s" % cmds.stderr.strip())
    # A third call for parents. Cheap, and it is what lets a process that
    # reports no path be attributed to whatever spawned it.
    ppids = parents(subprocess.run(PPID_CMD, capture_output=True, text=True,
                                   timeout=20).stdout)
    return [proc._replace(ppid=ppids.get(proc.pid, 0))
            for proc in combine(main.stdout, cmds.stdout)]
