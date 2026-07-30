"""Instantaneous readings for the live view, taken without writing anything.

The recorder samples every 30 seconds because that is the cadence worth
keeping for years. Watching a machine wants something faster, and the two
needs do not have to share a mechanism: this takes its own reading, diffs it
against its own in-memory baseline, and returns. Nothing here touches the
database, so the live view can update every couple of seconds without
inflating the history or fighting the sampler for the write lock.

`ps` plus libproc costs about 70 ms, which is affordable at that rate.
`nettop` costs five seconds, which is not, so network figures come from a
background refresh and are served slightly stale rather than holding up
every request.
"""
import threading
import time

from . import identity, netstat, psreader, rusage, system

# Below this the deltas are dominated by sampling jitter rather than real
# work, so the previous answer is reused instead of computing noise.
MIN_INTERVAL = 0.4

_lock = threading.Lock()
_prev = {"ts": None, "cpu": {}, "counters": {}}
_net = {"ts": 0, "data": {}}
_net_thread = None


def _refresh_network():
    """nettop in the background, forever. Five seconds per pass, so the live
    view gets numbers a few seconds old rather than waiting on them."""
    while True:
        try:
            data = netstat.read()
            with _lock:
                _net["data"] = data
                _net["ts"] = time.time()
        except Exception:
            pass
        time.sleep(20)


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
