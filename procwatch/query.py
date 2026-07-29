"""Read side. Shapes the JSON the dashboard consumes: avg and max both
surfaced (with the timestamp of the max, so a coarse bucket can still name
the minute a spike happened), percentages unscaled from their stored
integer-tenths form, the synthetic __other__ row flagged rather than hidden,
and a coverage ratio per system point so a mostly-asleep bucket reads
differently from a genuinely idle one.

Tiers are DISJOINT IN TIME, not nested: a row lives in exactly one tier and
only moves to the next when it expires out of the one below (see rollup.py).
raw holds roughly the last week, fine the month before that, and so on. A
requested window can straddle that boundary -- "last 24 hours" started
entirely in raw, but as raw's retention window slides it will eventually
draw from fine too. Picking a single tier by span width alone (as if wider
windows just meant coarser copies of the same data) returns nothing for any
window that falls outside whichever tier happens to hold data right now.
So every read here queries every tier that overlaps [start, end) and folds
what comes back into uniform render buckets, using the same merge rules
rollup.collapse uses when moving a bucket up a tier -- the read path must
not contradict the storage path.
"""
from . import config

# A window wider than this many buckets is too dense to render usefully.
TARGET_BUCKETS = 400

_TIER_NAMES = frozenset(tier.name for tier in config.TIERS)
_TIER_SECONDS = {tier.name: tier.seconds for tier in config.TIERS}


def pick_tier(span_seconds):
    """The render resolution for this span: the finest tier whose bucket
    count fits TARGET_BUCKETS. This only picks a bucket WIDTH to fold into --
    it no longer selects which table gets read; every tier overlapping the
    window is always read (see module docstring).
    """
    for tier in config.TIERS:
        if span_seconds / float(tier.seconds) <= TARGET_BUCKETS:
            return tier
    return config.TIERS[-1]


def _bucket_start(ts, seconds):
    return ts - (ts % seconds)


def _tier_union(prefix, select_cols, where_extra=""):
    """A UNION ALL of `select_cols` across every tier's <prefix><name> table,
    each filtered by ts BETWEEN a start/end placeholder pair. Safe against
    double-counting because the tiers are disjoint in time (see module
    docstring) -- a row read from one tier's table cannot also appear in
    another's.
    """
    parts = ["SELECT %s FROM %s%s WHERE ts >= ? AND ts < ?%s"
             % (select_cols, prefix, tier.name, where_extra) for tier in config.TIERS]
    return " UNION ALL ".join(parts)


def _proc_names(conn, ids):
    placeholders = ",".join("?" * len(ids))
    return {r[0]: (r[1], r[2], bool(r[3]), r[4] or "") for r in conn.execute(
        "SELECT id, exe, cmdline_full, is_system, app FROM proc WHERE id IN (%s)"
        % placeholders, ids).fetchall()}


# A synthetic identity for the folded remainder, distinct from any real
# proc.id (which are positive).
_OTHER_ID = -1


def _fold_samples(rows, names, bucket_width):
    """Merge rows (proc_id, ts, cpu_avg, cpu_max, cpu_max_ts, rss_avg,
    rss_max, nproc, samples) landing in the same (proc_id, render bucket)
    into one point per process per bucket, then group into per-process
    entries. `rows` must be ordered by ts ascending so a tie in cpu_max
    keeps the earliest timestamp, matching rollup.collapse's own
    strictly-greater comparison.
    """
    acc = {}
    for (pid, ts, cpu_avg, cpu_max, cpu_max_ts, rss_avg, rss_max, nproc, samples,
         net_in, net_out, disk_read, disk_write, energy, stuck) in rows:
        key = (pid, _bucket_start(ts, bucket_width))
        a = acc.get(key)
        if a is None:
            acc[key] = {"cpu_avg_sum": cpu_avg * samples, "cpu_max": cpu_max,
                        "cpu_max_ts": cpu_max_ts, "rss_avg_sum": rss_avg * samples,
                        "rss_max": rss_max, "nproc": nproc, "samples": samples,
                        # Byte and energy counters are totals for the interval,
                        # not rates, so they add. Rates are derived at render
                        # time by dividing by the bucket's own width.
                        "net_in": net_in, "net_out": net_out,
                        "disk_read": disk_read, "disk_write": disk_write,
                        "energy": energy, "stuck": stuck}
            continue
        a["cpu_avg_sum"] += cpu_avg * samples
        if cpu_max > a["cpu_max"]:
            a["cpu_max"], a["cpu_max_ts"] = cpu_max, cpu_max_ts
        a["rss_avg_sum"] += rss_avg * samples
        a["rss_max"] = max(a["rss_max"], rss_max)
        a["nproc"] = max(a["nproc"], nproc)
        a["samples"] += samples
        a["net_in"] += net_in
        a["net_out"] += net_out
        a["disk_read"] += disk_read
        a["disk_write"] += disk_write
        a["energy"] += energy
        a["stuck"] = max(a["stuck"], stuck)

    grouped = {}
    for (pid, bucket_ts), a in sorted(acc.items(), key=lambda kv: kv[0][1]):
        exe, cmdline, is_system, app = names[pid]
        entry = grouped.setdefault(pid, {
            "exe": exe, "cmdline": cmdline, "is_other": exe == config.OTHER,
            "is_system": is_system, "app": app, "points": []})
        entry["points"].append({
            "ts": bucket_ts,
            "cpu_avg": (a["cpu_avg_sum"] / a["samples"]) / 10.0,
            "cpu_max": a["cpu_max"] / 10.0,
            "cpu_max_ts": a["cpu_max_ts"],
            "rss_avg": a["rss_avg_sum"] / a["samples"],
            "rss_max": a["rss_max"],
            "nproc": a["nproc"],
            "net_in": a["net_in"], "net_out": a["net_out"],
            "disk_read": a["disk_read"], "disk_write": a["disk_write"],
            "energy": a["energy"], "stuck": a["stuck"]})
    return grouped


def _fold_system(rows, bucket_width, now=None):
    """Same idea as _fold_samples for system_<tier> rows: cpu_busy, load1,
    mem_used_kb, mem_comp_kb and swap_used_kb are sample-weighted, disk_free_kb
    is a true min.

    `expected` is NOT accumulated from the source rows. It is the nominal count
    a bucket of this width implies -- `bucket_width // INTERVAL` -- which is what
    `rollup.collapse` writes when it materialises a bucket. Summing the sources
    instead would make coverage the ratio of present samples to *present*
    expectation, which is 1.0 by construction: a five-minute bucket holding two
    ticks of a possible ten would read fully covered, exactly the mostly-asleep
    case the sub-0.5 hatching exists to reveal. It would also make the same
    window change appearance the night rollup crossed it, because the fold and
    the stored row would disagree.

    The bucket containing `now` is prorated to the wall clock actually elapsed
    within it, so a window ending mid-bucket does not read as a gap merely for
    not having happened yet.
    """
    nominal = max(1, bucket_width // config.INTERVAL)
    acc = {}
    for (ts, cpu_busy, load1, mem_used, mem_comp, swap_used, disk_free, samples,
         expected, batt_pct, batt_draw_mw, batt_full_mwh, on_ac) in rows:
        key = _bucket_start(ts, bucket_width)
        a = acc.get(key)
        if a is None:
            acc[key] = {"cpu_busy_sum": cpu_busy * samples, "load1_sum": load1 * samples,
                        "mem_used_sum": mem_used * samples, "mem_comp_sum": mem_comp * samples,
                        "swap_used_sum": swap_used * samples, "disk_free": disk_free,
                        "samples": samples, "batt_pct": batt_pct,
                        "draw_mw": batt_draw_mw, "full_mwh": batt_full_mwh,
                        "on_ac": on_ac}
            continue
        a["cpu_busy_sum"] += cpu_busy * samples
        a["load1_sum"] += load1 * samples
        a["mem_used_sum"] += mem_used * samples
        a["mem_comp_sum"] += mem_comp * samples
        a["swap_used_sum"] += swap_used * samples
        a["disk_free"] = min(a["disk_free"], disk_free)
        a["samples"] += samples
        if batt_pct >= 0:
            a["batt_pct"] = batt_pct
        if batt_draw_mw > 0:
            a["draw_mw"] = max(a["draw_mw"], batt_draw_mw)
        a["full_mwh"] = max(a["full_mwh"], batt_full_mwh)
        a["on_ac"] = min(a["on_ac"], on_ac)

    result = []
    for ts in sorted(acc):
        a = acc[ts]
        weight = a["samples"] or 1
        expected = nominal
        if now is not None and ts <= now < ts + bucket_width:
            elapsed = max(config.INTERVAL, now - ts)
            expected = max(1, min(nominal, elapsed // config.INTERVAL))
        result.append({
            "ts": ts,
            "cpu_busy": (a["cpu_busy_sum"] / weight) / 10.0,
            "load1": (a["load1_sum"] / weight) / 100.0,
            "mem_used_kb": a["mem_used_sum"] // weight,
            "mem_comp_kb": a["mem_comp_sum"] // weight,
            "swap_used_kb": a["swap_used_sum"] // weight,
            "disk_free_kb": a["disk_free"],
            "expected": expected,
            "samples": a["samples"],
            "batt_pct": a["batt_pct"],
            "batt_draw_mw": a["draw_mw"],
            "batt_full_mwh": a["full_mwh"],
            "on_ac": bool(a["on_ac"]),
            "coverage": min(1.0, a["samples"] / float(expected))})
    return result


def series(conn, start, end, limit=12, scope="all"):
    """Top-`limit` series by peak CPU in [start, end), plus system and gaps.

    scope="apps" ranks and returns APPLICATIONS: identities that belong to a
    .app bundle, folded together under the bundle's name. Ranking has to
    happen inside the scope, not before it -- taking the top twelve processes
    overall and then discarding the ones that are not applications returns
    nothing at all on a machine whose heaviest consumers are Spotlight and
    media analysis, which is most machines over a long enough window.
    """
    tier = pick_tier(max(end - start, 1))
    bucket_width = tier.seconds

    scope_filter = ""
    if scope == "apps":
        scope_filter = (" AND proc_id IN (SELECT id FROM proc WHERE app != '')")
    # The sampler's own remainder row never competes for a rank: it is folded
    # into the remainder computed below, so there is exactly one of them.
    rank_filter = scope_filter + (
        " AND proc_id NOT IN (SELECT id FROM proc WHERE exe = '%s')" % config.OTHER)
    rank_sql = ("SELECT proc_id, MAX(cpu_max) FROM (%s) GROUP BY proc_id "
                "ORDER BY 2 DESC LIMIT ?"
                % _tier_union("sample_", "proc_id, cpu_max", rank_filter))
    rank_params = []
    for _ in config.TIERS:
        rank_params.extend([start, end])
    rank_params.append(limit)
    ids = [r[0] for r in conn.execute(rank_sql, rank_params).fetchall()]

    # A window with no process samples (a sleep gap, or before any process
    # existed) is still a window that can carry system and gap rows -- those
    # are queried below regardless of whether any process ranked here.
    grouped = {}
    if ids:
        placeholders = ",".join("?" * len(ids))
        detail_cols = ("proc_id, ts, cpu_avg, cpu_max, cpu_max_ts, rss_avg, "
                       "rss_max, nproc, samples, net_in, net_out, disk_read, "
                       "disk_write, energy, stuck")
        detail_sql = ("SELECT %s FROM (%s) ORDER BY ts"
                       % (detail_cols,
                          _tier_union("sample_", detail_cols,
                                      " AND proc_id IN (%s)" % placeholders)))
        detail_params = []
        for _ in config.TIERS:
            detail_params.extend([start, end] + ids)
        rows = conn.execute(detail_sql, detail_params).fetchall()
        grouped = _fold_samples(rows, _proc_names(conn, ids), bucket_width)

    # Everything that did not make the cut, as one band.
    #
    # Without this the chart was a stack of the top `limit` processes and
    # nothing else: the sampler keeps forty per tick, so ranks thirteen to
    # forty were simply absent. The stack therefore did not add up to the
    # machine -- measured against the recorded system total it came to about
    # half -- and the energy chart, which normalises by the sum of what it was
    # given, reported shares of a subset as though they were shares of the
    # whole.
    #
    # Summed per timestamp in SQL first, so folding into render buckets is
    # then the ordinary time-fold rather than a sum across processes that
    # _fold_samples would have averaged.
    rest_filter = scope_filter
    rest_params_extra = []
    if ids:
        rest_filter += " AND proc_id NOT IN (%s)" % ",".join("?" * len(ids))
        rest_params_extra = ids
    rest_cols = ("ts, cpu_avg, cpu_max, cpu_max_ts, rss_avg, rss_max, nproc, "
                 "samples, net_in, net_out, disk_read, disk_write, energy, stuck")
    rest_sql = (
        "SELECT ts, SUM(cpu_avg), SUM(cpu_avg), MIN(ts), SUM(rss_avg), "
        "SUM(rss_max), SUM(nproc), MAX(samples), SUM(net_in), SUM(net_out), "
        "SUM(disk_read), SUM(disk_write), SUM(energy), MAX(stuck) "
        "FROM (%s) GROUP BY ts ORDER BY ts"
        % _tier_union("sample_", rest_cols, rest_filter))
    rest_params = []
    for _ in config.TIERS:
        rest_params.extend([start, end] + rest_params_extra)
    rest_rows = conn.execute(rest_sql, rest_params).fetchall()
    # cpu_max is reported as the sum of averages rather than a real maximum:
    # the peaks of a hundred unrelated processes did not happen at the same
    # instant, so any larger figure would be a number nothing ever reached.
    rest_rows = [(_OTHER_ID,) + tuple(r) for r in rest_rows if r[1]]
    if rest_rows:
        names = {_OTHER_ID: (config.OTHER, "", 1, "")}
        for pid, points in _fold_samples(rest_rows, names, bucket_width).items():
            grouped[pid] = points

    sys_cols = ("ts, cpu_busy, load1, mem_used_kb, mem_comp_kb, swap_used_kb, "
                "disk_free_kb, samples, expected, batt_pct, batt_draw_mw, "
                "batt_full_mwh, on_ac")
    system_sql = ("SELECT %s FROM (%s) ORDER BY ts"
                   % (sys_cols, _tier_union("system_", sys_cols)))
    system_params = []
    for _ in config.TIERS:
        system_params.extend([start, end])
    system_rows = conn.execute(system_sql, system_params).fetchall()
    system = _fold_system(system_rows, bucket_width, now=end)

    # ts_start <= ts_end always holds (db.py normalises backwards clock steps);
    # reason ('sleep' or 'clock') is passed through so the client can render
    # a clock rewind -- overwritten wall-clock time -- differently from a
    # genuine absence of data.
    gaps = [{"start": r[0], "end": r[1], "reason": r[2]} for r in conn.execute(
        "SELECT ts_start, ts_end, reason FROM gap WHERE ts_end >= ? AND ts_start < ? "
        "ORDER BY ts_start", (start, end)).fetchall()]

    # Ranked first, remainder last: the caller stacks smallest-first but reads
    # this order for colour, and the remainder is not a process competing for
    # a rank.
    order = [i for i in ids if i in grouped]
    if _OTHER_ID in grouped:
        order.append(_OTHER_ID)
    return {"tier": tier.name, "series": [grouped[i] for i in order],
            "system": system, "gaps": gaps}


def bucket_detail(conn, tier_name, ts):
    """Every process recorded in one render bucket, ranked by average CPU.

    tier_name can come from an HTTP query parameter, so it is checked
    against the known tiers before being interpolated into SQL. ts is
    floored to that tier's bucket width and every source tier overlapping
    [bucket_start, bucket_start + width) is read and folded -- the bucket a
    client is drilling into may already straddle a tier boundary, the same
    reason series() cannot read a single table either.
    """
    if tier_name not in _TIER_NAMES:
        raise ValueError("unknown tier: %r" % (tier_name,))
    width = _TIER_SECONDS[tier_name]
    start = _bucket_start(ts, width)
    end = start + width

    sql = ("SELECT proc_id, ts, cpu_avg, cpu_max, cpu_max_ts, rss_avg, rss_max, "
           "nproc, samples, net_in, net_out, disk_read, disk_write, energy, "
           "stuck FROM (%s) ORDER BY ts"
           % _tier_union("sample_", "proc_id, ts, cpu_avg, cpu_max, cpu_max_ts, "
                         "rss_avg, rss_max, nproc, samples, net_in, net_out, "
                         "disk_read, disk_write, energy, stuck"))
    params = []
    for _ in config.TIERS:
        params.extend([start, end])
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return []

    ids = sorted(set(r[0] for r in rows))
    grouped = _fold_samples(rows, _proc_names(conn, ids), width)

    out = []
    for entry in grouped.values():
        point = entry["points"][0]  # one process, one bucket -> exactly one point
        # cpu_max_ts travels with cpu_max. Drilling into a six-hour archive
        # bucket is precisely when "which minute did it peak" is the question
        # being asked, so dropping it here would defeat the tier design at the
        # one place it pays off most.
        out.append({"exe": entry["exe"], "cmdline": entry["cmdline"],
                     "is_other": entry["is_other"],
                     "is_system": entry.get("is_system", True),
                     # Needed to fold helpers under the application that owns
                     # them, the same way the charts group them.
                     "app": entry.get("app", ""),
                     "cpu_avg": point["cpu_avg"],
                     "cpu_max": point["cpu_max"], "cpu_max_ts": point["cpu_max_ts"],
                     "rss_kb": point["rss_avg"], "rss_max_kb": point["rss_max"],
                     "nproc": point["nproc"], "net_in": point["net_in"],
                     "net_out": point["net_out"], "disk_read": point["disk_read"],
                     "disk_write": point["disk_write"], "energy": point["energy"],
                     "stuck": point["stuck"]})
    out.sort(key=lambda r: r["cpu_avg"], reverse=True)
    return out


def activity(conn, start, end):
    """Machine busyness per hour, for the calendar heatmap.

    Deliberately its own query rather than a coarse `series` call: the
    heatmap wants one number per wall-clock hour across weeks, which is a
    different shape from "top processes over a window" and would otherwise
    pull every per-process row in the range across the wire to be thrown
    away.

    Reads every tier, since a month-long span straddles them, and reports
    coverage per hour so an hour the machine was asleep renders as absent
    rather than as idle -- those are not the same thing and the whole point
    of the grid is spotting the difference.
    """
    sql = ("SELECT ts, cpu_busy, samples, expected FROM (%s) ORDER BY ts"
           % _tier_union("system_", "ts, cpu_busy, samples, expected"))
    params = []
    for _ in config.TIERS:
        params.extend([start, end])

    hours = {}
    for ts, cpu_busy, samples, expected in conn.execute(sql, params):
        hour = ts - (ts % 3600)
        a = hours.setdefault(hour, {"sum": 0, "n": 0, "peak": 0, "samples": 0})
        a["sum"] += cpu_busy * samples
        a["n"] += samples
        a["peak"] = max(a["peak"], cpu_busy)
        a["samples"] += samples

    nominal = 3600 // config.INTERVAL
    return [{"ts": hour,
             "cpu_avg": (a["sum"] / a["n"]) / 10.0 if a["n"] else 0.0,
             "cpu_max": a["peak"] / 10.0,
             "coverage": min(1.0, a["samples"] / float(nominal))}
            for hour, a in sorted(hours.items())]


def _like(term):
    """A LIKE pattern that matches `term` anywhere, with the wildcards in it
    treated as literal text.

    Without escaping, searching for "_" matches every single character and
    "%" matches everything -- so the two characters a user is most likely to
    type when looking for a path or a percentage would return the whole
    database.
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + escaped + "%"


def search(conn, term, limit=25, now=None):
    """Everything ever recorded whose name, application or command matches.

    Searches the identity table rather than the samples: identities are
    interned once and number in the thousands, where samples number in the
    millions. The samples are then consulted only for the handful of
    identities that matched, to say when each was last seen and how hard it
    ever worked -- which is what makes a result worth clicking.
    """
    term = (term or "").strip()
    if not term:
        return []
    rows = conn.execute(
        "SELECT id, exe, args_sig, cmdline_full, is_system, app FROM proc "
        "WHERE exe LIKE ? ESCAPE '\\' OR app LIKE ? ESCAPE '\\' "
        "   OR cmdline_full LIKE ? ESCAPE '\\' "
        # An exact name first, then a name that starts with the term, then
        # anything else: typing "arc" should not lead with a command line that
        # happens to mention it.
        "ORDER BY (LOWER(exe) = LOWER(?)) DESC, "
        "         (LOWER(exe) LIKE LOWER(?) || '%') DESC, exe "
        "LIMIT ?",
        (_like(term), _like(term), _like(term), term, term, limit * 4)).fetchall()
    if not rows:
        return []

    ids = [r[0] for r in rows]
    placeholders = ",".join("?" * len(ids))
    stats_sql = (
        "SELECT proc_id, MAX(ts), MAX(cpu_max), MAX(rss_max), SUM(samples) "
        "FROM (%s) GROUP BY proc_id"
        % " UNION ALL ".join(
            "SELECT proc_id, ts, cpu_max, rss_max, samples FROM sample_%s "
            "WHERE proc_id IN (%s)" % (tier.name, placeholders)
            for tier in config.TIERS))
    params = []
    for _ in config.TIERS:
        params.extend(ids)
    stats = {r[0]: r[1:] for r in conn.execute(stats_sql, params).fetchall()}

    out = []
    for pid, exe, args_sig, cmdline, is_system, app in rows:
        stat = stats.get(pid)
        if not stat:
            # Interned but never sampled: nothing to show and nothing to click
            # through to.
            continue
        last_ts, cpu_max, rss_max, samples = stat
        out.append({
            "exe": exe, "app": app or "", "cmdline": cmdline,
            "args": args_sig, "is_system": bool(is_system),
            "last_ts": last_ts, "cpu_max": (cpu_max or 0) / 10.0,
            "rss_max": rss_max or 0, "samples": samples or 0,
        })
    # Ranked by how well the name matches first, and only then by how hard it
    # worked. Sorting on CPU alone would answer "Arc" with whatever process
    # was busiest that happens to mention arc in its command line, which is
    # never what the person typing a name meant.
    lowered = term.lower()

    def rank(row):
        name = row["exe"].lower()
        app = (row["app"] or "").lower()
        if name == lowered:
            tier = 0                      # the process you named
        elif app == lowered:
            tier = 1                      # its helpers, which share the app
        elif name.startswith(lowered):
            tier = 2
        elif app.startswith(lowered) or lowered in name or lowered in app:
            tier = 3
        else:
            tier = 4                      # matched only in the command line
        return (tier, -row["cpu_max"], -row["last_ts"])

    out.sort(key=rank)
    return out[:limit]
