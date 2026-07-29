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

from . import (alerts, config, db, identity, netstat, psreader, rollup,
               rusage, sampler, storage, system)


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
        sampler.tick(conn, procs, readings, now, extra, battery,
                     identity.classify(procs), identity.apps(procs))
        rollup.run(conn, now)
        rollup.disk_guard(conn, now, system.free_bytes())
        # Rules are checked against the rows just written. A rule that cannot
        # be evaluated must not cost the tick its sample, which is the only
        # thing here that cannot be recovered later.
        try:
            for event in alerts.evaluate(conn, now):
                alerts.notify(event)
        except Exception as error:
            _log("alert evaluation failed: %s" % error)
        # Disk usage is a size, not a rate: it moves once a week and costs a
        # filesystem walk to measure, so it runs once a day. Last, so a slow
        # walk cannot delay the sample -- that is already written and
        # committed by this point.
        try:
            if storage.due(conn):
                storage.scan(conn)
        except Exception as error:
            _log("storage scan failed: %s" % error)
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
