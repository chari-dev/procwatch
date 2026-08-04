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

from . import (alerts, config, db, diagnose, events, identity, netpeer,
               netstat, power, psreader, rollup, rusage, sampler, selfupdate,
               storage, system, versions)


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

    # Network and disk are best-effort: nettop reports only processes that
    # touched the network, and libproc refuses other users' processes. Neither
    # is worth failing a tick over, so a failure here costs those columns and
    # nothing else -- CPU and memory still record.
    extra = {}
    try:
        net = netstat.read()
        ru = rusage.read_all([p.pid for p in procs])
        for proc in procs:
            n = net.get(proc.pid, (0, 0))
            r = ru.get(proc.pid)
            extra[proc.pid] = (n[0], n[1],
                               r[0] if r else 0, r[1] if r else 0,
                               r[4] if r else 0)
    except Exception as error:
        _log("extra counters unavailable this tick: %s" % error)
        extra = {}

    try:
        battery = system.battery()
    except Exception:
        battery = None

    conn = None
    try:
        conn = db.connect(config.DB_PATH)
        db.init_schema(conn)
        # The first tick of a new version is what makes an update real, so it
        # is what gets recorded and announced -- however the code arrived.
        try:
            updated = selfupdate.note_if_updated(conn, now)
            if updated:
                _log("procwatch updated: %s -> %s"
                     % (updated["from"], updated["to"]))
                alerts.announce(conn, "Procwatch updated",
                                "Now running %s (was %s)"
                                % (updated["to"], updated["from"]),
                                target="events", now=now)
        except Exception as error:
            _log("version note failed: %s" % error)
        sampler.tick(conn, procs, readings, now, extra, battery,
                     identity.classify(procs), identity.apps(procs))
        # Who the machine was talking to, so the map can be asked about a time
        # rather than only about now. A second nettop pass rather than reuse of
        # the one above: that one runs with -P, which reports per-process totals
        # and deliberately hides the sockets underneath them. Both passes cost
        # about 20 ms since the reverse-DNS wait was removed.
        try:
            netpeer.record(conn, netstat.traffic(), now)
            netpeer.prune(conn, now)
        except Exception as error:
            _log("peer history unavailable this tick: %s" % error)
        rollup.run(conn, now)
        rollup.disk_guard(conn, now, system.free_bytes())
        # Rules are checked against the rows just written. A rule that cannot
        # be evaluated must not cost the tick its sample, which is the only
        # thing here that cannot be recovered later.
        try:
            for event in alerts.evaluate(conn, now):
                # Queued rather than posted: the menu bar app collects these
                # and shows them as Procwatch, and a click opens the process
                # the alert is about. deliver_stale below is the fallback.
                title, body = alerts.announcement(event)
                alerts.announce(conn, title, body,
                                target="find=" + event["exe"], now=now)
        except Exception as error:
            _log("alert evaluation failed: %s" % error)
        try:
            alerts.deliver_stale(
                conn, now, wait=alerts.STALE_AFTER
                if alerts.bar_running() else 0)
        except Exception as error:
            _log("note delivery failed: %s" % error)
        # Disk usage is a size, not a rate: it moves once a week and costs a
        # filesystem walk to measure, so it runs once a day. Last, so a slow
        # walk cannot delay the sample -- that is already written and
        # committed by this point.
        try:
            if storage.due(conn):
                storage.scan(conn)
        except Exception as error:
            _log("storage scan failed: %s" % error)
        # What is holding the machine awake, every tick: 13 ms, and the only
        # way to know the duration of a hold that ends between two ticks.
        try:
            power.tick(conn, now)
            # The full power log costs ten seconds, so it is read rarely and
            # only for what observation cannot see: what happened while this
            # was not running, because the machine was asleep.
            if power.due(conn, now):
                power.import_log(conn, now)
                power.prune(conn, now)
        except Exception as error:
            _log("power collection failed: %s" % error)
        # Crashes, boots, installs and the rest, every quarter of an hour.
        # Cheap -- two directory listings, `last`, and one system_profiler --
        # but pointless every thirty seconds: a Mac does not crash twice a
        # minute, and when it does, the reports are still there next tick.
        try:
            events.init(conn)
            if events.due(conn, now):
                events.collect(conn, now)
                events.prune(conn, now=now)
        except Exception as error:
            _log("event collection failed: %s" % error)
        # What happened, worked out on a slow cadence so it can be mentioned
        # without anybody having to go and look. Last of the cheap collectors:
        # it reads the tables the ones above just wrote, so it wants to run
        # after them rather than beside them.
        try:
            if diagnose.watch_due(conn, now):
                said = lambda title, body: alerts.announce(
                    conn, title, body, target="why", now=now)
                for finding in diagnose.watch(conn, now, post=said):
                    _log("finding: %s" % finding["headline"])
        except Exception as error:
            _log("finding watch failed: %s" % error)
        # Versions ride along with the daily storage scan: reading a hundred
        # Info.plist files is cheap but pointless to repeat every thirty
        # seconds, and an app updates a few times a month at most.
        try:
            if storage.due(conn, now, every=3600):
                versions.tick(conn, now)
                versions.prune(conn, now)
        except Exception as error:
            _log("version collection failed: %s" % error)
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
