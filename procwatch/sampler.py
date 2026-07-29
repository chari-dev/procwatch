# procwatch/sampler.py
"""One tick: read, difference, aggregate, select, write.

CPU is a rate computed from cumulative-time deltas. It is never read from
`ps %cpu`, which is a decaying average over an opaque window (`man ps`),
does not reconcile against a system total, and cannot be integrated across
buckets -- which is exactly what the rollup arithmetic does.
"""
import collections

from . import config, identity

Agg = collections.namedtuple(
    "Agg", "cpu rss nproc cmdline net_in net_out disk_read disk_write energy stuck",
    defaults=("", 0, 0, 0, 0, 0, 0))

# A process that has burned a whole core for this many consecutive ticks while
# reading and writing nothing at all is doing no work anything can observe.
# macOS has no supported way to ask whether an app is servicing its run loop --
# Activity Monitor uses a private interface -- so this is a heuristic and is
# reported as "possibly stuck", never as fact.
STUCK_CPU = 95.0
STUCK_TICKS = 4


def cpu_percent(prev_cs, cur_cs, dt):
    """Rate over a known interval, or None when the reading is unusable."""
    if dt <= 0:
        return None
    delta_cs = cur_cs - prev_cs
    if delta_cs < 0:
        return None
    return (delta_cs / 100.0) / dt * 100.0


def _delta(prev, cur):
    """Difference two cumulative counters, discarding a counter that went
    backwards -- which means the pid was recycled or the source reset."""
    if prev is None or cur is None or cur < prev:
        return 0
    return cur - prev


def aggregate(procs, prev_state, now, dt, extra=None, prev_extra=None,
              stuck_runs=None):
    """Fold processes into per-identity totals; return them and the new state.

    prev_state maps pid -> (start_time, cputime_cs) from the previous tick.
    The returned state map covers every pid in `procs` regardless of whether
    it had a usable previous reading -- callers rely on this to refresh
    sampler_state even on a first-sighting or post-gap tick.

    `extra` and `prev_extra` map pid -> (net_in, net_out, disk_read,
    disk_write, energy) of CUMULATIVE counters, this tick and last. Like
    cputime they are differenced, never read raw. A pid missing from either
    contributes zero rather than dropping the process, since network and disk
    coverage is partial by nature: nettop reports only processes that touched
    the network, and libproc refuses other users' processes.
    """
    extra = extra or {}
    prev_extra = prev_extra or {}
    stuck_runs = stuck_runs or {}
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

        cur_x, prev_x = extra.get(proc.pid), prev_extra.get(proc.pid)
        if cur_x and prev_x:
            deltas = tuple(_delta(prev_x[i], cur_x[i]) for i in range(5))
        else:
            deltas = (0, 0, 0, 0, 0)
        busy_idle = cpu >= STUCK_CPU and deltas[2] == 0 and deltas[3] == 0
        stuck = 1 if busy_idle and stuck_runs.get(proc.pid, 0) + 1 >= STUCK_TICKS else 0

        key = identity.derive(proc.comm, proc.command)
        current = aggs.get(key)
        if current is None:
            aggs[key] = Agg(cpu, proc.rss_kb, 1, proc.command, *deltas, stuck)
        else:
            aggs[key] = Agg(current.cpu + cpu,
                            current.rss + proc.rss_kb,
                            current.nproc + 1,
                            current.cmdline,
                            current.net_in + deltas[0],
                            current.net_out + deltas[1],
                            current.disk_read + deltas[2],
                            current.disk_write + deltas[3],
                            current.energy + deltas[4],
                            max(current.stuck, stuck))
    return aggs, state


def _sum_field(rows, name):
    return sum(getattr(a, name) for a in rows)


def select(aggs):
    """Split into the recorded set and a single __other__ remainder.

    Ranked by the union of top CPU, top memory, top network and top disk, so a
    process that is quiet on CPU but saturating the network still gets its own
    row instead of vanishing into the remainder.
    """
    keys = set()
    for field in ("cpu", "rss", "net_in", "net_out", "disk_read", "disk_write"):
        ranked = sorted(aggs, key=lambda k: getattr(aggs[k], field), reverse=True)
        # Rank by a metric only where something actually used it. Otherwise a
        # tick in which nothing touched the network still reserves TOP_N slots
        # for "top network users", pulling in arbitrary processes and emptying
        # the __other__ remainder that makes the chart reconcile.
        ranked = [k for k in ranked[:config.TOP_N] if getattr(aggs[k], field) > 0]
        keys |= set(ranked)
    kept = {k: aggs[k] for k in keys}
    rest = [aggs[k] for k in aggs if k not in keys]
    other = Agg(_sum_field(rest, "cpu"), _sum_field(rest, "rss"),
                _sum_field(rest, "nproc"), "",
                _sum_field(rest, "net_in"), _sum_field(rest, "net_out"),
                _sum_field(rest, "disk_read"), _sum_field(rest, "disk_write"),
                _sum_field(rest, "energy"), 0)
    return kept, other


def _proc_id(conn, exe, args_sig, cmdline_full, is_system=True, app=""):
    """Intern an identity, recording whether it is part of macOS.

    The flag is decided here because this is the only place the parent chain
    is available -- a process that reports no path (postgres renames its
    workers, autofsd exposes nothing) can only be resolved by looking at what
    spawned it, and the history keeps no parent.
    """
    row = conn.execute(
        "SELECT id, is_system, app FROM proc WHERE exe = ? AND args_sig = ?",
        (exe, args_sig)).fetchone()
    if row:
        if app and not row[2]:
            conn.execute("UPDATE proc SET app = ? WHERE id = ?", (app, row[0]))
        # Correct a stored flag when this reading knows better. The backfill
        # for pre-existing identities only had the command line, which is a
        # bare name for anything that rewrote its own argv, so it had to leave
        # those as macOS. Here the parent chain is available, so a "yours"
        # verdict is better information and replaces it.
        if row[1] and not is_system:
            conn.execute("UPDATE proc SET is_system = 0 WHERE id = ?", (row[0],))
        return row[0]
    cur = conn.execute(
        "INSERT INTO proc (exe, args_sig, cmdline_full, is_system, app) "
        "VALUES (?,?,?,?,?)",
        (exe, args_sig, cmdline_full, 1 if is_system else 0, app or ""))
    return cur.lastrowid


def _load_state(conn):
    """Only the newest tick's rows.

    MAX(updated_ts) over the whole table can name a row left behind by a
    process that exited, whose timestamp may even be in the future after a
    backwards clock step. Taking dt from one tick and the cputime baseline
    from another invents a spike.
    """
    rows = conn.execute(
        "SELECT pid, start_time, cputime_cs FROM sampler_state "
        "WHERE updated_ts = (SELECT MAX(updated_ts) FROM sampler_state)").fetchall()
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
    conn.execute("DELETE FROM sampler_state WHERE updated_ts < ?",
                 (now - config.STATE_TTL,))


def _load_extra(conn):
    """{pid: (net_in, net_out, disk_read, disk_write, energy)} from last tick."""
    rows = conn.execute(
        "SELECT pid, net_in, net_out, disk_read, disk_write, energy, stuck_run "
        "FROM sampler_extra").fetchall()
    return ({r[0]: tuple(r[1:6]) for r in rows},
            {r[0]: r[6] for r in rows})


def _save_extra(conn, extra, aggs_stuck, now):
    conn.executemany(
        "INSERT INTO sampler_extra (pid, net_in, net_out, disk_read, disk_write, "
        "energy, stuck_run, updated_ts) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(pid) DO UPDATE SET net_in=excluded.net_in, "
        "net_out=excluded.net_out, disk_read=excluded.disk_read, "
        "disk_write=excluded.disk_write, energy=excluded.energy, "
        "stuck_run=excluded.stuck_run, updated_ts=excluded.updated_ts",
        [(pid, v[0], v[1], v[2], v[3], v[4], aggs_stuck.get(pid, 0), now)
         for pid, v in extra.items()])
    conn.execute("DELETE FROM sampler_extra WHERE updated_ts < ?",
                 (now - config.STATE_TTL,))


def tick(conn, procs, readings, now, extra=None, batt=None,
         system_pids=None, app_pids=None):
    """Record one sample. Safe to call on an empty or a populated database.

    aggregate()'s state map is built solely from `procs` -- it does not
    depend on prev_state or dt -- so one call produces both the correct
    aggregates (empty when there is nothing to diff against) and the
    correct state to persist, on the first tick, a post-gap tick, and a
    normal tick alike.

    `extra` is {pid: (net_in, net_out, disk_read, disk_write, energy)} of
    cumulative counters gathered this tick. Omitting it records CPU and memory
    only, which is what happens when nettop or libproc are unavailable.
    """
    extra = extra or {}
    system_pids = system_pids or {}

    # An identity is macOS only if every process under it is.
    system_by_key, app_by_key = {}, {}
    app_pids = app_pids or {}
    for proc in procs:
        key = identity.derive(proc.comm, proc.command)
        flag = system_pids.get(proc.pid, True)
        system_by_key[key] = system_by_key.get(key, True) and flag
        if not app_by_key.get(key):
            app_by_key[key] = app_pids.get(proc.pid) or ""

    with conn:
        previous_ts = _last_tick(conn)
        prev_state = _load_state(conn)
        prev_extra, stuck_runs = _load_extra(conn)
        dt = None if previous_ts is None else now - previous_ts

        if dt is not None and (dt < 0 or dt > config.INTERVAL * config.GAP_FACTOR):
            reason = "clock" if dt < 0 else "sleep"
            conn.execute(
                "INSERT OR IGNORE INTO gap (ts_start, ts_end, reason) VALUES (?,?,?)",
                (min(previous_ts, now), max(previous_ts, now), reason))
            prev_state = {}   # the delta across a sleep or clock step is meaningless
            prev_extra = {}

        # dt only matters when prev_state is non-empty (a normal tick); on
        # the first-ever tick prev_state is already {}. 0.0 makes cpu_percent
        # return None unconditionally rather than relying on prev_state being
        # empty to make the placeholder harmless.
        aggs, state = aggregate(procs, prev_state, now,
                                dt if dt is not None else 0.0,
                                extra, prev_extra, stuck_runs)

        # Carry the consecutive-busy-and-idle run forward per pid so the
        # heuristic needs STUCK_TICKS in a row rather than one unlucky sample.
        runs = {}
        for proc in procs:
            cur_x, prev_x = extra.get(proc.pid), prev_extra.get(proc.pid)
            prev_cpu = prev_state.get(proc.pid)
            if not (cur_x and prev_x and prev_cpu) or dt is None:
                continue
            cpu = cpu_percent(prev_cpu[1], proc.cputime_cs, dt)
            quiet = (_delta(prev_x[2], cur_x[2]) == 0
                     and _delta(prev_x[3], cur_x[3]) == 0)
            if cpu is not None and cpu >= STUCK_CPU and quiet:
                runs[proc.pid] = stuck_runs.get(proc.pid, 0) + 1

        if aggs:
            kept, other = select(aggs)
            rows = []
            for (exe, sig), agg in list(kept.items()) + [((config.OTHER, ""), other)]:
                fallback = exe if sig == "" else exe + " " + sig
                pid_row = _proc_id(conn, exe, sig, agg.cmdline or fallback,
                                   system_by_key.get((exe, sig), True),
                                   app_by_key.get((exe, sig), ""))
                cpu = int(round(agg.cpu * 10))
                rows.append((now, pid_row, cpu, cpu, now,
                             agg.rss, agg.rss, agg.nproc, 1,
                             agg.net_in, agg.net_out, agg.disk_read,
                             agg.disk_write, agg.energy, agg.stuck))
            conn.executemany(
                "INSERT OR REPLACE INTO sample_raw (ts, proc_id, cpu_avg, cpu_max, "
                "cpu_max_ts, rss_avg, rss_max, nproc, samples, net_in, net_out, "
                "disk_read, disk_write, energy, stuck) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            conn.execute(
                "INSERT OR REPLACE INTO system_raw (ts, cpu_busy, load1, mem_used_kb, "
                "mem_comp_kb, swap_used_kb, disk_free_kb, samples, expected, "
                "batt_pct, batt_draw_mw, batt_full_mwh, on_ac) "
                "VALUES (?,?,?,?,?,?,?,1,1,?,?,?,?)",
                (now, int(round(sum(a.cpu for a in aggs.values()) * 10)),
                 readings.load1, readings.mem_used_kb, readings.mem_comp_kb,
                 readings.swap_used_kb, readings.disk_free_kb,
                 (batt or {}).get("percent", -1) if batt else -1,
                 (batt or {}).get("draw_mw") if (batt and batt.get("draw_mw")) else -1,
                 (batt or {}).get("full_mwh", 0) or 0,
                 1 if (batt or {}).get("on_ac", True) else 0))

        _save_state(conn, state, now)
        if extra:
            _save_extra(conn, extra, runs, now)
