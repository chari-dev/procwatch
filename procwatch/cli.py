"""procwatch install | open | serve | record | backup | restore | alert |
status | uninstall"""
import argparse
import os
import sys
import time

from . import alerts, archive, config, db, launchd


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


def _restore(path, assume_yes):
    """Replace the recorded history with a file, after saying what that means.

    This is the only command that can destroy data, so unless it is told not
    to it describes both databases and waits for a yes. The one being
    replaced is copied aside regardless.
    """
    print("restore from  %s\n              %s" % (path, archive.describe(path)))
    if os.path.exists(config.DB_PATH):
        print("replacing     %s\n              %s"
              % (config.DB_PATH, archive.describe(config.DB_PATH)))
    if not assume_yes:
        try:
            if input("\nReplace the recorded history? [y/N] ").strip().lower() \
                    not in ("y", "yes"):
                print("nothing changed")
                return 1
        except EOFError:
            print("no answer; nothing changed", file=sys.stderr)
            return 1
    try:
        target, previous = archive.restore(path)
    except (RuntimeError, OSError) as error:
        print("restore failed: %s" % error, file=sys.stderr)
        return 1
    print("restored %s" % target)
    if previous:
        print("the replaced database is at %s" % previous)
    return 0


def _alert(args):
    """Add, list or remove a rule.

    This command used to write to a `watchlist` table that nothing ever read.
    It has been a no-op in every release; now it does what its name promised.
    """
    config.ensure_dirs()
    conn = db.connect(config.DB_PATH)
    db.init_schema(conn)
    alerts.init(conn)
    try:
        if args.remove is not None:
            if not alerts.remove(conn, args.remove):
                print("no rule with id %d" % args.remove, file=sys.stderr)
                return 1
        elif args.pattern is not None:
            alerts.add(conn, args.pattern, args.metric, args.threshold,
                       args.sustain)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    current = alerts.rules(conn)
    if not current:
        print("no rules. Add one:\n"
              "  procwatch alert '*' --metric cpu --above 80 --for 10m")
        return 0
    for rule in current:
        print("%-4d %-20s %-7s above %g%s for %s%s"
              % (rule["id"], rule["pattern"], rule["metric"], rule["threshold"],
                 alerts.METRICS[rule["metric"]]["unit"],
                 alerts._duration(rule["sustain"]),
                 "" if rule["enabled"] else "  (disabled)"))
    return 0


def _duration_arg(text):
    """"10m", "2h", "45s" or bare seconds -- because nobody thinks in seconds."""
    text = str(text).strip().lower()
    units = {"s": 1, "m": 60, "h": 3600}
    if text and text[-1] in units:
        return int(float(text[:-1]) * units[text[-1]])
    return int(float(text))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="procwatch")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install")
    sub.add_parser("uninstall")
    sub.add_parser("status")
    # The launchd agent's entry point. Named rather than reached through
    # `-m procwatch.main`, because the single-file build has no package
    # directory for -m to find and the agent must work from either form.
    sub.add_parser("record")
    opener = sub.add_parser("open")
    opener.add_argument("--port", type=int, default=8787)
    # Serving without opening a browser or reaping an idle server: the menu
    # bar app owns the lifetime, and a closed panel is not a reason to stop.
    server_cmd = sub.add_parser("serve")
    server_cmd.add_argument("--port", type=int, default=8790)
    saver = sub.add_parser("backup")
    saver.add_argument("path", nargs="?", default=".",
                       help="file, or a directory to put a dated file in")
    loader = sub.add_parser("restore")
    loader.add_argument("path")
    loader.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt")
    alerter = sub.add_parser("alert")
    alerter.add_argument("pattern", nargs="?",
                         help="process or application name, or * for anything")
    alerter.add_argument("--metric", default="cpu",
                         choices=sorted(alerts.METRICS))
    alerter.add_argument("--above", dest="threshold", type=float, default=80.0)
    alerter.add_argument("--for", dest="sustain", type=_duration_arg,
                         default="10m",
                         help="how long it must hold: 30s, 10m, 2h")
    alerter.add_argument("--remove", type=int, metavar="ID")

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
    if args.command == "backup":
        try:
            written = archive.backup(args.path)
        except (RuntimeError, OSError) as error:
            print("backup failed: %s" % error, file=sys.stderr)
            return 1
        print("%s\n%s" % (written, archive.describe(written)))
        return 0
    if args.command == "restore":
        return _restore(args.path, args.yes)
    if args.command == "alert":
        return _alert(args)
    if args.command == "record":
        from . import main as recorder
        return recorder.run_once()
    if args.command == "serve":
        from . import server
        return server.serve(args.port, open_browser=False, idle_timeout=None)
    if args.command == "open":
        from . import server
        return server.serve(args.port)
    return 1


if __name__ == "__main__":
    sys.exit(main())
