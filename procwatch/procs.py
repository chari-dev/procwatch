"""Live process tree and the one mutating operation in the tool: signalling.

Everything else procwatch does is read-only and historical. This module is
what turns "that process has been eating a core for an hour" into something
you can act on, so it is deliberately the narrowest surface in the codebase:
one listing function and one signal function, both refusing anything the
calling user does not own.
"""
import os
import signal as signal_module
import subprocess

from . import identity, netstat, rusage

# TERM asks, KILL compels. Nothing else is exposed: the point is to stop a
# runaway process, not to offer a general signal console.
# STOP and CONT are here for the network monitor's off switch. Blocking one
# application's traffic properly needs a network extension Apple has to
# authorise, which this tool cannot hold -- but a suspended process cannot
# run, and a process that cannot run cannot send or receive anything. It is
# a blunter instrument than a firewall rule and it is reversible, which is
# the pair of properties that makes it honest to offer.
ALLOWED_SIGNALS = {"TERM": signal_module.SIGTERM, "KILL": signal_module.SIGKILL,
                   "STOP": signal_module.SIGSTOP, "CONT": signal_module.SIGCONT}

PS_FIELDS = ["ps", "-Axo", "pid,ppid,uid,pcpu,rss,state,lstart,comm"]


def _own_uid():
    return os.getuid()


def live_tree():
    """Every process this user owns, grouped by application.

    Grouping is by the same identity rule the recorder uses, so what appears
    here as one row is the same thing that appears as one series in the
    history -- 27 renderers of a browser are one entry with a child count,
    not 27 rows that push everything else off the screen.

    The parent chain is carried so a helper can be attributed to the app that
    spawned it: a browser's renderers name the browser, not launchd.
    """
    out = subprocess.run(["ps", "-Axo", "pid,ppid,uid,pcpu,rss,state,comm"],
                         capture_output=True, text=True, timeout=20)
    if out.returncode != 0:
        return {"groups": [], "error": "ps failed"}
    commands = {}
    cmd_out = subprocess.run(["ps", "-Axo", "pid,command"],
                             capture_output=True, text=True, timeout=20)
    for line in cmd_out.stdout.splitlines()[1:]:
        pid_text, _, rest = line.strip().partition(" ")
        if pid_text.isdigit():
            commands[int(pid_text)] = rest.strip()

    uid = _own_uid()
    rows, parents = [], {}
    for line in out.stdout.splitlines()[1:]:
        parts = line.split(None, 6)
        if len(parts) < 7 or not parts[0].isdigit():
            continue
        pid, ppid, puid = int(parts[0]), int(parts[1]), int(parts[2])
        parents[pid] = ppid
        if puid != uid:
            continue
        comm = parts[6].strip()
        rows.append({"pid": pid, "ppid": ppid, "cpu": float(parts[3]),
                     "rss_kb": int(parts[4]), "state": parts[5],
                     "comm": comm, "command": commands.get(pid, comm)})

    counters = rusage.read_all([r["pid"] for r in rows])
    net = netstat.read()
    ports = {}
    for row in netstat.listeners():
        ports.setdefault(row["pid"], []).append(row["port"])

    groups = {}
    for row in rows:
        exe, sig = identity.derive(row["comm"], row["command"])
        entry = groups.setdefault((exe, sig), {
            "exe": exe, "args": sig, "command": row["command"],
            "is_system": identity.is_system(row["comm"]),
            "cpu": 0.0, "rss_kb": 0, "pids": [], "ports": [],
            "disk_read": 0, "disk_write": 0, "net_in": 0, "net_out": 0,
            "energy": 0, "waiting": 0})
        entry["cpu"] += row["cpu"]
        entry["rss_kb"] += row["rss_kb"]
        entry["pids"].append(row["pid"])
        entry["ports"].extend(ports.get(row["pid"], []))
        # 'U' is an uninterruptible wait -- blocked in the kernel, usually on
        # I/O. It is the closest public signal to "not responding", and is
        # reported as a count rather than a verdict.
        if row["state"].startswith("U"):
            entry["waiting"] += 1
        counter = counters.get(row["pid"])
        if counter:
            entry["disk_read"] += counter[0]
            entry["disk_write"] += counter[1]
            entry["energy"] += counter[4]
        seen = net.get(row["pid"])
        if seen:
            entry["net_in"] += seen[0]
            entry["net_out"] += seen[1]

    listing = []
    for entry in groups.values():
        entry["nproc"] = len(entry["pids"])
        entry["ports"] = sorted(set(entry["ports"]))
        # The lowest pid in a group is the one whose parent is outside it --
        # the app itself rather than one of its helpers -- so it is the pid a
        # "quit" should be aimed at.
        entry["lead_pid"] = min(entry["pids"])
        listing.append(entry)
    listing.sort(key=lambda e: e["cpu"], reverse=True)
    return {"groups": listing, "uid": uid}


def running_now(procs=None):
    """Which of the processes recorded in the past are running right now.

    Returns {(exe, args_sig): [pid, ...]} and {app: [pid, ...]}, derived through
    identity.derive -- the same function the sampler used when it wrote the
    history, so a row from a week ago and a process alive this second are keyed
    the same way and either match or genuinely differ.

    This exists because the history does not record PIDs, and should not: a PID
    is reused within hours, so a button offering to end "the process that was
    running at 4:15pm" by its number would eventually end something else
    entirely, with the same confident label. Matching by identity means the
    button can only ever act on something that is running now and is the same
    program -- and when nothing matches there is nothing to press.
    """
    from . import psreader
    procs = psreader.read() if procs is None else procs
    by_identity, by_app = {}, {}
    apps = identity.apps(procs)
    for proc in procs:
        exe, sig = identity.derive(proc.comm, proc.command)
        by_identity.setdefault((exe, sig), []).append(proc.pid)
        app = apps.get(proc.pid)
        if app:
            by_app.setdefault(app, []).append(proc.pid)
    return by_identity, by_app


def signal_pid(pid, name="TERM"):
    """Send TERM or KILL to one process this user owns.

    Refuses another user's process rather than letting the kernel refuse it,
    so the caller gets an explanation instead of an errno, and refuses pid 1
    outright -- signalling launchd is never what anyone meant.
    """
    if name not in ALLOWED_SIGNALS:
        return {"ok": False, "error": "signal must be one of %s"
                                      % ", ".join(sorted(ALLOWED_SIGNALS))}
    if pid <= 1:
        return {"ok": False, "error": "refusing to signal pid %d" % pid}
    try:
        stat = subprocess.run(["ps", "-o", "uid=,comm=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "error": "could not look up pid %d" % pid}
    if stat.returncode != 0 or not stat.stdout.strip():
        return {"ok": False, "error": "no such process: %d" % pid}
    parts = stat.stdout.split(None, 1)
    if int(parts[0]) != _own_uid():
        return {"ok": False,
                "error": "pid %d belongs to another user" % pid}
    comm = (parts[1].strip() if len(parts) > 1 else "").split("/")[-1]
    try:
        os.kill(pid, ALLOWED_SIGNALS[name])
    except ProcessLookupError:
        return {"ok": False, "error": "process %d already gone" % pid}
    except PermissionError:
        return {"ok": False, "error": "not permitted to signal %d" % pid}
    return {"ok": True, "pid": pid, "signal": name, "comm": comm}


def signal_group(pids, name="TERM"):
    """Signal every pid in a group, reporting each outcome separately.

    A partial failure is normal -- helpers exit on their own once the parent
    goes -- so this never aborts on the first error.
    """
    return [signal_pid(pid, name) for pid in pids]
