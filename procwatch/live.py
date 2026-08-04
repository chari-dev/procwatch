"""Instantaneous readings for the live view, taken without writing anything.

The recorder samples every 30 seconds because that is the cadence worth
keeping for years. Watching a machine wants something faster, and the two
needs do not have to share a mechanism: this takes its own reading, diffs it
against its own in-memory baseline, and returns. Nothing here touches the
database, so the live view can update every couple of seconds without
inflating the history or fighting the sampler for the write lock.

`ps` plus libproc costs about 70 ms, which is affordable at that rate.
`nettop` used to cost five seconds -- all of it reverse-DNS, fixed by passing
-n (see netstat.NETTOP) -- and now costs about 20 ms. The background refresh
below is kept anyway: it is what lets consecutive passes be differenced into
rates, and a request should still never be the thing that runs a subprocess.
"""
import os
import plistlib
import socket
import subprocess
import threading
import time

from . import geoip, icons, identity, netstat, psreader, rusage, system

# Below this the deltas are dominated by sampling jitter rather than real
# work, so the previous answer is reused instead of computing noise.
MIN_INTERVAL = 0.4

_lock = threading.Lock()
_prev = {"ts": None, "cpu": {}, "counters": {}}
_net = {"ts": 0, "data": {}, "traffic": [], "rates": {}, "asked": 0.0}
_net_thread = None


def rate_deltas(prev, cur, dt):
    """{pid: (in_per_s, out_per_s)} from two total readings.

    A pid whose counters went backwards is a reused pid or a restarted
    process; its history belongs to something else, so it reports nothing
    rather than a negative rate or an enormous false one.
    """
    if not prev or dt <= 0:
        return {}
    out = {}
    for pid, (got_in, got_out) in cur.items():
        was = prev.get(pid)
        if was is None or got_in < was[0] or got_out < was[1]:
            continue
        out[pid] = ((got_in - was[0]) / dt, (got_out - was[1]) / dt)
    return out


def _refresh_network():
    """nettop in the background, forever, so no request ever runs a subprocess.

    One nettop run carries two answers: the per-process totals the live table
    has always used, and the per-connection detail the network panel shows.
    Rates come from differencing consecutive passes, which is the reason this
    stays a loop now that a pass is cheap -- a single reading of a cumulative
    counter is not a rate.
    """
    prev_totals, prev_ts = None, None
    while True:
        try:
            traffic = netstat.traffic()
            totals = {t["pid"]: (t["bytes_in"], t["bytes_out"])
                      for t in traffic}
            now = time.time()
            rates = rate_deltas(prev_totals, totals,
                                now - prev_ts if prev_ts else 0)
            prev_totals, prev_ts = totals, now
            with _lock:
                _net["data"] = totals
                _net["traffic"] = traffic
                _net["rates"] = rates
                _net["ts"] = now
        except Exception:
            pass
        # Faster while somebody is actually watching the wire: the monitor
        # page polls network_traffic, and that is the signal.
        #
        # A pass used to take five seconds, so "sleep 6" meant nettop was
        # running 45% of the time whenever the monitor had been open in the
        # last minute, and 20% of the time when it had not. That was the
        # server's real idle cost. A pass now takes about 20 ms, so these
        # same numbers are a 0.3% duty cycle, and the unwatched cadence can
        # be slower still without the rates going stale.
        with _lock:
            watched = time.time() - _net["asked"] < 60
        time.sleep(6 if watched else 30)


# Peer names, resolved off to the side. nettop is run numeric (-x) because
# inline resolution is what makes it slow; the panel wants names anyway, so
# they are looked up here, once each, and served from the cache -- a peer
# shows as its address until its name is known, which beats every request
# waiting on a resolver that may have nothing to say.
_DNS = {}
_DNS_QUEUE = []
_dns_thread = None


def _resolve_forever():
    while True:
        with _lock:
            host = _DNS_QUEUE.pop(0) if _DNS_QUEUE else None
        if host is None:
            time.sleep(2)
            continue
        try:
            name = socket.gethostbyaddr(host)[0]
        except OSError:
            name = ""
        with _lock:
            _DNS[host] = name


def peer_name(host):
    """The reverse-DNS name for a peer, or "" until one is known."""
    global _dns_thread
    if not host or host.startswith("*"):
        return ""
    with _lock:
        if host in _DNS:
            return _DNS[host]
        if host not in _DNS_QUEUE:
            _DNS_QUEUE.append(host)
    if _dns_thread is None:
        _dns_thread = threading.Thread(target=_resolve_forever, daemon=True)
        _dns_thread.start()
    return ""


# Who owns which pid, read once per call rather than per process: `ps` is a
# process launch and forty of them is a visible pause.
def _owners():
    """{pid: (user, stopped)} -- who owns each process, and whether it is
    suspended. The state letter is read here rather than guessed at, so the
    monitor's off switch reports what is actually true of the machine even
    when something else did the suspending."""
    try:
        done = subprocess.run(["ps", "-Axo", "pid,user,state"],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out = {}
    for line in done.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 3 and parts[0].isdigit():
            out[int(parts[0])] = (parts[1], parts[2].startswith("T"))
    return out


def executable_path(command):
    """argv[0] out of a command line, which is the program on disk.

    Paths contain spaces ("/Applications/Visual Studio Code.app/..."), so the
    longest leading run that exists as a file wins over the first token --
    otherwise every application with a space in its name reports a truncated
    path that resolves to nothing.
    """
    command = (command or "").strip()
    if not command:
        return ""
    if os.path.isfile(command):
        return command
    parts = command.split(" ")
    for count in range(len(parts), 0, -1):
        candidate = " ".join(parts[:count])
        if os.path.isfile(candidate):
            return candidate
    # A bare word is a command name, not a path. Returning it anyway put
    # "claude" in a field labelled Path, which reads as a location on disk
    # and is not one.
    return parts[0] if parts[0].startswith("/") else ""


_IDENT_CACHE = {}


def code_identity(exe_path):
    """The bundle identifier behind an executable, when there is one.

    /Applications/Arc.app/Contents/MacOS/Arc -> company.thebrowser.Browser.
    Cached: this reads a plist off disk, and the answer for a path does not
    change while the program is running.
    """
    if not exe_path:
        return ""
    if exe_path in _IDENT_CACHE:
        return _IDENT_CACHE[exe_path]
    ident = ""
    marker = ".app/Contents/MacOS/"
    if marker in exe_path:
        bundle = exe_path[:exe_path.index(marker) + 4]
        try:
            with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as fh:
                ident = plistlib.load(fh).get("CFBundleIdentifier") or ""
        except Exception:
            ident = ""
    _IDENT_CACHE[exe_path] = ident
    return ident


def network_traffic():
    """Who is talking to the network right now, grouped by application.

    Little-Snitch-shaped, minus the blocking: per application, bytes in and
    out per second and over each process's lifetime, every live connection
    with its peer and roughly where that peer is, and enough about the
    program itself -- path, identifier, owner -- to say what it is. Read from
    the background nettop pass, so this answers instantly with numbers at
    most a refresh old.
    """
    with _lock:
        traffic = list(_net["traffic"])
        rates = dict(_net["rates"])
        age = time.time() - _net["ts"] if _net["ts"] else None
        _net["asked"] = time.time()

    lookup = geoip.enabled()
    procs = psreader.read()
    apps = identity.apps(procs)
    system_pids = identity.classify(procs)
    users = _owners()
    label = {}
    for proc in procs:
        exe, _sig = identity.derive(proc.comm, proc.command)
        label[proc.pid] = (apps.get(proc.pid) or exe,
                           system_pids.get(proc.pid, True),
                           executable_path(proc.command))

    groups = {}
    for row in traffic:
        name, is_system, path = label.get(row["pid"],
                                          (row["name"], True, ""))
        entry = groups.setdefault(name, {
            "app": name, "is_system": is_system, "pids": [],
            "in_rate": 0.0, "out_rate": 0.0,
            "bytes_in": 0, "bytes_out": 0, "conns": [],
            "path": path, "code_id": code_identity(path),
            "bundle": icons.bundle_of(path),
            "user": (users.get(row["pid"]) or ("", False))[0],
            "suspended": True})
        entry["is_system"] = entry["is_system"] and is_system
        entry["pids"].append(row["pid"])
        # Suspended only when every one of its processes is: a browser with
        # one frozen renderer is not an application that has been stopped.
        entry["suspended"] = entry["suspended"] and \
            (users.get(row["pid"]) or ("", False))[1]
        entry["bytes_in"] += row["bytes_in"]
        entry["bytes_out"] += row["bytes_out"]
        if not entry["path"] and path:
            entry["path"] = path
            entry["code_id"] = code_identity(path)
            entry["bundle"] = icons.bundle_of(path)
        rate = rates.get(row["pid"])
        if rate:
            entry["in_rate"] += rate[0]
            entry["out_rate"] += rate[1]
        for conn in row["conns"]:
            named = dict(conn)
            named["peer"] = peer_name(conn["host"])
            named["where"] = geoip.where(conn["host"], named["peer"],
                                         allow_lookup=lookup)
            entry["conns"].append(named)

    listing = sorted(groups.values(),
                     key=lambda e: -(e["in_rate"] + e["out_rate"]
                                     or (e["bytes_in"] + e["bytes_out"]) * 1e-12))
    for entry in listing:
        entry["conns"].sort(key=lambda c: -(c["bytes_in"] + c["bytes_out"]))
        entry["conns"] = entry["conns"][:40]
    return {"apps": listing,
            "age": round(age, 1) if age is not None else None,
            "total_in": sum(e["in_rate"] for e in listing),
            "total_out": sum(e["out_rate"] for e in listing),
            # Where this Mac is, so the globe puts the near end of every route
            # in the right place rather than guessing from the peers.
            "here": geoip.own(allow_lookup=lookup),
            "lookup": lookup}


def start_network_refresh():
    global _net_thread
    if _net_thread is None:
        _net_thread = threading.Thread(target=_refresh_network, daemon=True)
        _net_thread.start()


def snapshot():
    """Current per-application rates, grouped the way the history groups them.

    The first call establishes a baseline and reports zero rates -- there is
    nothing to difference against yet. Every call after that is a real rate
    over the actual elapsed interval, which is why the interval is returned:
    a caller polling irregularly should know what window it is looking at.
    """
    now = time.time()
    procs = psreader.read()
    counters = rusage.read_all([p.pid for p in procs])
    system_pids = identity.classify(procs)
    app_pids = identity.apps(procs)

    with _lock:
        prev_ts = _prev["ts"]
        prev_cpu = _prev["cpu"]
        prev_counters = _prev["counters"]
        net = dict(_net["data"])
        net_age = now - _net["ts"] if _net["ts"] else None

    dt = (now - prev_ts) if prev_ts else 0.0
    usable = dt >= MIN_INTERVAL

    ports = {}
    for row in netstat.listeners():
        ports.setdefault(row["pid"], []).append(row["port"])

    groups = {}
    cpu_state, counter_state = {}, {}
    for proc in procs:
        cpu_state[proc.pid] = (proc.start_time, proc.cputime_cs)
        counter_state[proc.pid] = counters.get(proc.pid)

        cpu = 0.0
        was = prev_cpu.get(proc.pid)
        if usable and was and was[0] == proc.start_time:
            delta_cs = proc.cputime_cs - was[1]
            if delta_cs >= 0:
                cpu = (delta_cs / 100.0) / dt * 100.0

        cur, old = counters.get(proc.pid), prev_counters.get(proc.pid)
        read_rate = write_rate = power = 0.0
        if usable and cur and old:
            read_rate = max(0, cur[0] - old[0]) / dt
            write_rate = max(0, cur[1] - old[1]) / dt
            # Energy is a cumulative counter like the disk ones, and it was
            # being reported raw: a process that had been running for a week
            # showed a bigger number than one melting a core right now, and
            # sorting by the column sorted by age. As a rate it is power, which
            # is the question anybody reading that column is asking.
            #
            # ri_billed_energy is nanojoules, so nanojoules per second is
            # nanowatts, and a millionth of that is milliwatts.
            power = max(0, cur[4] - old[4]) / dt / 1e6

        exe, sig = identity.derive(proc.comm, proc.command)
        entry = groups.setdefault((exe, sig), {
            "exe": exe, "args": sig, "command": proc.command,
            "is_system": system_pids.get(proc.pid, True),
            "app": app_pids.get(proc.pid) or "",
            "cpu": 0.0, "rss_kb": 0, "pids": [], "ports": [],
            "disk_read": 0.0, "disk_write": 0.0,
            "net_in": 0, "net_out": 0, "energy": 0, "waiting": 0})
        entry["is_system"] = entry["is_system"] and system_pids.get(proc.pid, True)
        if not entry["app"]:
            entry["app"] = app_pids.get(proc.pid) or ""
        entry["cpu"] += cpu
        entry["rss_kb"] += proc.rss_kb
        entry["pids"].append(proc.pid)
        entry["ports"].extend(ports.get(proc.pid, []))
        entry["disk_read"] += read_rate
        entry["disk_write"] += write_rate
        entry["energy"] += power
        seen = net.get(proc.pid)
        if seen:
            entry["net_in"] += seen[0]
            entry["net_out"] += seen[1]

    with _lock:
        _prev["ts"] = now
        _prev["cpu"] = cpu_state
        _prev["counters"] = counter_state

    listing = []
    for entry in groups.values():
        entry["nproc"] = len(entry["pids"])
        entry["ports"] = sorted(set(entry["ports"]))
        entry["lead_pid"] = min(entry["pids"])
        listing.append(entry)
    listing.sort(key=lambda e: e["cpu"], reverse=True)

    readings = system.read()
    batt = system.battery()
    return {
        "ts": int(now),
        "interval": round(dt, 2),
        "warming_up": not usable,
        "groups": listing,
        "network_age": round(net_age, 1) if net_age is not None else None,
        "system": {
            "cpu_busy": sum(e["cpu"] for e in listing),
            "load1": readings.load1 / 100.0,
            "mem_used_kb": readings.mem_used_kb,
            "mem_comp_kb": readings.mem_comp_kb,
            "swap_used_kb": readings.swap_used_kb,
            "disk_free_kb": readings.disk_free_kb,
        },
        "battery": batt,
    }
