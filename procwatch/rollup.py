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
        "SELECT DISTINCT ts / ? FROM (SELECT ts FROM %s WHERE ts < ? "
        "UNION SELECT ts FROM %s WHERE ts < ?) ORDER BY 1 LIMIT ?"
        % (sample_src, system_src),
        (coarser.seconds, cutoff, cutoff, config.ROLLUP_BATCH)).fetchall()]
    if not boundaries:
        return 0

    with conn:
        for index in boundaries:
            low = index * coarser.seconds
            high = low + coarser.seconds
            rows = conn.execute(
                "SELECT proc_id, cpu_avg, cpu_max, cpu_max_ts, rss_avg, rss_max, "
                "nproc, samples, net_in, net_out, disk_read, disk_write, "
                "energy, stuck FROM %s WHERE ts >= ? AND ts < ?" % sample_src,
                (low, high)).fetchall()

            merged = {}
            for (pid, cpu_avg, cpu_max, cpu_max_ts, rss_avg, rss_max, nproc, samples,
                 net_in, net_out, disk_read, disk_write, energy, stuck) in rows:
                acc = merged.get(pid)
                if acc is None:
                    # Byte and energy counters are TOTALS for the bucket, not rates,
                    # so they add rather than average. stuck is a flag: a
                    # bucket is suspect if any source bucket was.
                    merged[pid] = [cpu_avg * samples, cpu_max, cpu_max_ts,
                                   rss_avg * samples, rss_max, nproc, samples,
                                   net_in, net_out, disk_read, disk_write,
                                   energy, stuck]
                    continue
                acc[0] += cpu_avg * samples
                if cpu_max > acc[1]:
                    acc[1], acc[2] = cpu_max, cpu_max_ts
                acc[3] += rss_avg * samples
                acc[4] = max(acc[4], rss_max)
                acc[5] = max(acc[5], nproc)
                acc[6] += samples
                acc[7] += net_in
                acc[8] += net_out
                acc[9] += disk_read
                acc[10] += disk_write
                acc[11] += energy
                acc[12] = max(acc[12], stuck)

            conn.executemany(
                "INSERT INTO %s (ts, proc_id, cpu_avg, cpu_max, cpu_max_ts, rss_avg, "
                "rss_max, nproc, samples, net_in, net_out, disk_read, "
                "disk_write, energy, stuck) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(ts, proc_id) DO UPDATE SET "
                "cpu_avg=excluded.cpu_avg, cpu_max=excluded.cpu_max, "
                "cpu_max_ts=excluded.cpu_max_ts, rss_avg=excluded.rss_avg, "
                "rss_max=excluded.rss_max, nproc=excluded.nproc, "
                "samples=excluded.samples" % sample_dst,
                [(low, pid, acc[0] // acc[6], acc[1], acc[2],
                  acc[3] // acc[6], acc[4], acc[5], acc[6],
                  acc[7], acc[8], acc[9], acc[10], acc[11], acc[12])
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


def prune_tier(conn, tier, now):
    """Drop anything past this tier's window. The last tier has none."""
    if tier.retain_seconds is None:
        return
    cutoff = now - tier.retain_seconds
    with conn:
        conn.execute("DELETE FROM sample_%s WHERE ts < ?" % tier.name, (cutoff,))
        conn.execute("DELETE FROM system_%s WHERE ts < ?" % tier.name, (cutoff,))


def prune(conn, now):
    for tier in config.TIERS:
        prune_tier(conn, tier, now)


def run(conn, now):
    for finer, coarser in _tier_pairs():
        moved = collapse(conn, finer, coarser, now)
        if moved < config.ROLLUP_BATCH:
            # A partial batch means this tier's backlog just fully drained --
            # safe to prune, and safe to keep rolling the next pair upward.
            prune_tier(conn, finer, now)
        else:
            # A full batch means this tier still has a backlog. Rolling the
            # tier above now would hand it a coarser bucket whose sources are
            # only half present, and the next tick's upsert would overwrite
            # rather than merge -- erasing whatever the first pass recorded.
            break


def disk_guard(conn, now, free_bytes):
    """Below the floor, coarsen early rather than delete.

    Deleting a tier's rows outright destroys history that never reached any
    coarser resolution, and because each tier's halved window is more
    aggressive than its own collapse threshold, sustained pressure starves
    coarse and archive of everything. Collapsing instead compacts ten raw
    rows into one fine row -- more space freed, and only resolution is lost.

    This tool exists because background processes quietly consumed a
    machine. It will not be the process that fills a nearly-full disk, and
    it will not be the process that silently erases the record either.
    """
    if free_bytes >= config.MIN_FREE_BYTES:
        return False
    for finer, coarser in _tier_pairs():
        if finer.retain_seconds is None:
            continue
        # Treat rows as older than they are, halving the tier's effective
        # window so its data moves up instead of being deleted.
        early = now + finer.retain_seconds // 2
        while collapse(conn, finer, coarser, early) == config.ROLLUP_BATCH:
            pass
    return True
