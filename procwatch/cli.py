"""procwatch why | sleep | growth | updates | install | open | serve | record |
share | key | peer | app | backup | restore | alert | status | uninstall"""
import argparse
import json
import os
import sys
import time

from . import (alerts, appbuild, archive, config, db, diagnose, events,
               knowledge, launchd, peers, power, prefs, query, share, space,
               storage, versions)


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


def _fetch(path, query):
    """Print one API answer as JSON. This is what a peer runs when asked."""
    from urllib.parse import parse_qs
    from . import server
    conn = db.connect(config.DB_PATH)
    try:
        payload = server.api_get(conn, path, parse_qs(query))
    except ValueError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    finally:
        conn.close()
    if payload is None:
        print(json.dumps({"error": "unknown path %s" % path}), file=sys.stderr)
        return 1
    print(json.dumps(payload))
    return 0


def _peer(args):
    if args.action == "add":
        if not args.name or not args.host:
            print("usage: procwatch peer add <name> <ssh-host>", file=sys.stderr)
            return 1
        try:
            where = peers.add(args.name, args.host, args.key)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 1
        if not args.key:
            print("added %s at %s, but with no key -- it will be refused.\n"
                  "Run `procwatch share` on that machine and pass the three "
                  "words it prints with --key." % (args.name, where),
                  file=sys.stderr)
    elif args.action == "remove":
        if not args.name or not peers.remove(args.name):
            print("no peer called %r" % args.name, file=sys.stderr)
            return 1
    elif args.action == "check":
        names = [args.name] if args.name else [p["name"] for p in peers.listing()]
        if not names:
            print("no peers yet")
            return 0
        for name in names:
            state = peers.check(name)
            if state["ok"]:
                age = state["last_tick_age"]
                print("%-16s ok  %s  last sample %s"
                      % (name, state.get("hostname") or "",
                         "%ds ago" % age if age is not None else "never"))
            else:
                print("%-16s unreachable  %s" % (name, state["error"]))
        return 0

    current = peers.listing()
    if not current:
        print("no peers. Add one:\n"
              "  procwatch peer add laptop user@host.example")
        return 0
    for peer in current:
        print("%-16s %s" % (peer["name"], peer["host"]))
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


def _span(text):
    """"30m", "2h", "1d" or bare seconds."""
    text = str(text).strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if text and text[-1] in units:
        return int(float(text[:-1]) * units[text[-1]])
    return int(float(text))


def _at(text, now):
    """A time to look at. "14:32" means today at that time."""
    text = (text or "").strip()
    if not text:
        return now
    if ":" in text:
        parts = [int(p) for p in text.split(":")]
        today = time.localtime(now)
        wanted = time.struct_time((today.tm_year, today.tm_mon, today.tm_mday,
                                   parts[0], parts[1] if len(parts) > 1 else 0,
                                   parts[2] if len(parts) > 2 else 0,
                                   0, 0, -1))
        stamp = int(time.mktime(wanted))
        # A time later than now means yesterday: "why was it slow at 23:40"
        # asked over breakfast is about last night.
        return stamp - 86400 if stamp > now else stamp
    return int(float(text))


def _why(args):
    conn = db.connect(config.DB_PATH)
    try:
        now = int(time.time())
        end = _at(args.at, now)
        span = _span(args.span)
        result = diagnose.explain(conn, end - span, end, now=now)
    finally:
        conn.close()
    when = "%s to %s" % (time.strftime("%H:%M", time.localtime(result["start"])),
                         time.strftime("%H:%M", time.localtime(result["end"])))
    print("\n%s\n%s\n" % (result["verdict"], when))
    if not result["findings"]:
        return 0
    for finding in result["findings"]:
        mark = {"cause": "!", "cost": "-", "note": " "}.get(finding["severity"], " ")
        print(" %s %s" % (mark, finding["headline"]))
        for line in _wrap(finding["detail"], 74):
            print("     %s" % line)
        if finding["advice"]:
            for line in _wrap("-> " + finding["advice"], 74):
                print("     %s" % line)
        print()
    return 0


def _space(args):
    """`procwatch space` -- where the disk went.

    The scan is the slow part and everything else is a view of it, so running
    this without arguments scans if there is nothing stored and reports what it
    finds either way.
    """
    conn = db.connect(config.DB_PATH)
    try:
        vol = space.volumes()["data"]
        print("\n%s of %s used (%.0f%% full), %s free\n"
              % (_size(vol["used"]), _size(vol["total"]), vol["percent"],
                 _size(vol["free"])))

        snaps = space.snapshots()
        if snaps:
            print("  %d Time Machine snapshot%s are holding space on this disk."
                  % (len(snaps), "" if len(snaps) == 1 else "s"))
            print("  macOS thins them under pressure; they are not yours to "
                  "delete.\n")

        found = space.latest(conn)
        if args.scan or not found or not found["finished_ts"]:
            print("  Walking %s. This takes a few minutes.\n"
                  % (args.root or "~"))
            started = time.time()

            def tick(files, size):
                print("\r  %,d files, %s so far" .replace(",d", "d")
                      % (files, _size(size)), end="", flush=True)

            space.scan(conn, args.root, progress=tick)
            print("\r  done in %d seconds%s" % (time.time() - started, " " * 20))
            found = space.latest(conn)

        sid = found["id"]
        print("\n  Scanned %s: %s across %s files\n"
              % (found["root"].replace(os.path.expanduser("~"), "~"),
                 _size(found["bytes"]), "{:,}".format(found["files"])))

        print("  Biggest folders")
        for row in space.biggest_dirs(conn, sid, under=args.under, limit=12):
            about = space.explain(row["path"])
            name = row["path"].replace(os.path.expanduser("~"), "~")
            print("   %9s  %s%s" % (_size(row["bytes"]), name,
                                    "  [safe to clear]" if about and about["safe"]
                                    else ""))

        print("\n  By kind")
        for row in space.kinds(conn, sid)[:8]:
            print("   %9s  %-12s %s files"
                  % (_size(row["bytes"]), row["kind"],
                     "{:,}".format(row["files"])))

        owners = space.owners(conn, sid, limit=8)
        if owners:
            print("\n  By application")
            for row in owners:
                print("   %9s  %s" % (_size(row["bytes"]), row["owner"]))

        print("\n  Biggest single files")
        for row in space.biggest_files(conn, sid, limit=8):
            print("   %9s  %s" % (_size(row["bytes"]),
                                  row["path"].replace(os.path.expanduser("~"), "~")))
        print()
    finally:
        conn.close()
    return 0


def _caches(args):
    """`procwatch caches` -- what can be cleared, and clearing it."""
    found = space.caches()
    if not found:
        print("\nNothing on the safe-to-clear list is taking any space.\n")
        return 0
    total = sum(c["bytes"] for c in found)
    print("\n%s in caches that can be rebuilt\n" % _size(total))
    for cache in found:
        print("  %9s  %s" % (_size(cache["bytes"]), cache["path"]))
        for line in _wrap(cache["why"], 68):
            print("             %s" % line)
    if not args.clear:
        print("\nAdd --clear to move all of these to the Trash.\n")
        return 0

    print("\nMoving %d to the Trash." % len(found))
    results = space.trash([c["full_path"] for c in found])
    for result in results:
        if not result["ok"]:
            print("  kept %s: %s" % (result["path"], result["error"]))
    moved = sum(1 for r in results if r["ok"])
    print("Moved %d of %d. Nothing is gone until you empty the Trash.\n"
          % (moved, len(results)))
    return 0


def _findings(args):
    """`procwatch findings` -- and whether to be told about them.

    The settings live in the database rather than in the browser because the
    thing that acts on them is the recorder, and a launchd agent cannot read a
    browser's storage.
    """
    conn = db.connect(config.DB_PATH)
    try:
        prefs.init(conn)
        if args.off or args.on:
            was = prefs.findings_on(conn)
            prefs.set(conn, "findings_enabled", "0" if args.off else "1")
            if was and args.off:
                diagnose.forget_findings(conn)
        if args.notify:
            prefs.set(conn, "findings_notify", args.notify)
        state = prefs.all_prefs(conn)
        if not args.off and not args.on and not args.notify:
            # Nothing to change: show what happened, which is the question
            # somebody typing this without arguments is asking.
            now = int(time.time())
            found = diagnose.explain(conn, now - _span(args.span), now, now=now)
            print("\n%s\n" % found["verdict"])
            for finding in found["findings"]:
                mark = {"cause": "!", "cost": "-"}.get(finding["severity"], " ")
                print(" %s %s" % (mark, finding["headline"]))
            if found["findings"]:
                print()
    finally:
        conn.close()

    print("  working out what happened: %s"
          % ("on" if state["findings_enabled"] == "1" else "off"))
    print("  notifications: %s" % {
        "off": "none",
        "causes": "only findings that name a likely cause",
        "all": "every finding worth reporting",
    }[state["findings_notify"]])
    return 0


def _events(args):
    """`procwatch events` -- what has happened, not what has been measured.

    Ordered by what a person would ask in sequence: the paragraph, then the
    things that keep happening, then the things that have never happened
    before, then the incidents themselves. The raw timeline is last and behind
    a flag, because a list of four hundred events is the thing this exists to
    save somebody from reading.
    """
    conn = db.connect(config.DB_PATH)
    try:
        now = int(time.time())
        end, span = now, _span(args.span)
        events.init(conn)
        if events.due(conn, now, every=60):
            events.collect(conn, now)
        digest = events.digest(conn, end - span, end)
        line = events.timeline(conn, end - span, end, limit=200) if args.all else []
    finally:
        conn.close()

    print("\n%s\n" % "\n".join(_wrap(digest["summary"], 74)))

    if digest["patterns"]:
        print("  Keeps happening")
        for pattern in digest["patterns"]:
            print("   %s" % pattern["headline"])
            for text in _wrap(events.describe_pattern(pattern, now), 70):
                print("     %s" % text)
        print()

    if digest["firsts"]:
        print("  First time")
        for row in digest["firsts"]:
            print("   %s  %s" % (
                time.strftime("%d %b %H:%M", time.localtime(row["ts"])),
                row["headline"]))
        print()

    if digest["episodes"]:
        print("  Incidents")
        for group in digest["episodes"][:12]:
            mark = {"fault": "!", "cost": "-", "change": "+"}.get(
                group["severity"], " ")
            print(" %s %s  %s" % (
                mark,
                time.strftime("%d %b %H:%M", time.localtime(group["start"])),
                group["headline"]))
            for text in group["also"]:
                print("        also %s" % text)
            if group["more"]:
                print("        and %d more" % group["more"])
        print()

    for row in line:
        print(" %s  %-13s %s" % (
            time.strftime("%d %b %H:%M", time.localtime(row["ts"])),
            row["kind"], row["headline"]))
    if line:
        print()
    return 0


def _what(args):
    """`procwatch what WindowServer` -- what a process is, and what it does here.

    Two halves, and the second is the one that cannot be looked up: the
    catalogue says what WindowServer is, and this machine's own recording says
    what WindowServer normally costs on this machine. A number is only strange
    against a normal.
    """
    conn = db.connect(config.DB_PATH)
    try:
        entry = knowledge.describe(args.name, app=args.name)
        usual = query.usual(conn, args.name)
    finally:
        conn.close()

    print("\n%s  (%s)" % (entry["name"], entry["process"] or args.name))
    if not entry["known"]:
        print("  Not in the catalogue -- what follows is a deduction, not a fact.")
    print()
    for label, text in (("", entry["does"]),
                        ("When it is busy", entry["high"]),
                        ("What to do", entry["advice"])):
        if not text:
            continue
        lines = _wrap(text, 72)
        if label:
            print("  %s:" % label)
            for line in lines:
                print("    %s" % line)
        else:
            for line in lines:
                print("  %s" % line)
        print()

    if not usual:
        print("  Never seen on this Mac.\n")
        return 0
    print("  On this Mac: normally %.1f%% of a core and %s, with a peak of "
          "%.0f%% and %s."
          % (usual["cpu_avg"], _size(usual["memory_mb"] * 1e6),
             usual["cpu_peak"], _size(usual["memory_peak_mb"] * 1e6)))
    print("  %d samples, first seen %s.\n"
          % (usual["samples"],
             time.strftime("%d %b %Y", time.localtime(usual["first_seen"]))))
    return 0


def _wrap(text, width):
    words, lines, current = str(text).split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = (current + " " + word).strip()
    if current:
        lines.append(current)
    return lines


def _sleep(args):
    conn = db.connect(config.DB_PATH)
    try:
        now = int(time.time())
        start = now - _span(args.span)
        holding = power.holding_now(conn)
        held = power.kept_awake(conn, start, now)
        drain = power.overnight_drain(conn, start, now)
        spans = power.nights(conn, start, now)
    finally:
        conn.close()

    if drain:
        print("\nAsleep %s, woke %d times, battery fell %d%%."
              % (_hours(drain["asleep_seconds"]), drain["wakes"],
                 drain["charge_lost"]))
    else:
        print("\nNo battery-powered sleep recorded in this window.")

    reasons = {}
    for span in spans:
        if span.get("woke_because"):
            reasons[span["woke_because"]] = reasons.get(span["woke_because"], 0) + 1
    if reasons:
        print("\nWhat woke it:")
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1])[:6]:
            print("  %-34s %d" % (reason, count))

    if held:
        print("\nWhat kept it awake:")
        for hold in held[:6]:
            print("  %-18s %-10s %s" % (hold["process"], _hours(hold["seconds"]),
                                        ", ".join(hold["kinds"])[:38]))
    if holding:
        print("\nHolding it awake right now:")
        for hold in holding[:6]:
            if hold["prevents_sleep"]:
                print("  %-18s %-10s %s" % (hold["process"],
                                            _hours(hold["seconds"]), hold["kind"]))
    print()
    return 0


def _hours(seconds):
    seconds = int(seconds or 0)
    if seconds >= 3600:
        return "%.1f h" % (seconds / 3600.0)
    return "%d min" % (seconds // 60)


def _size(count):
    count = float(count or 0)
    for unit, size in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if abs(count) >= size:
            return "%.1f %s" % (count / size, unit)
    return "%d B" % count


def _growth(args):
    conn = db.connect(config.DB_PATH)
    try:
        now = int(time.time())
        report = storage.growth(conn, since=now - _span(args.span), now=now)
    finally:
        conn.close()
    if report["days_compared"] < 2:
        print("\nNot enough measurements yet -- disk usage is measured once a "
              "day, so this needs two days.\n")
        return 0
    print("\n%s %s since %s.\n"
          % (_size(abs(report["total_change"])),
             "more used" if report["total_change"] > 0 else "freed",
             time.strftime("%d %b", time.localtime(report["from_day"]))))
    for app in report["apps"]:
        sign = "+" if app["change"] > 0 else ""
        note = "" if app["state"] == "changed" else "  (%s)" % app["state"]
        print("  %-26s %s%s%s" % (app["app"], sign, _size(app["change"]), note))
    print()
    return 0


def _updates(args):
    conn = db.connect(config.DB_PATH)
    try:
        now = int(time.time())
        since = now - _span(args.span)
        changed = versions.updates(conn, since=since, now=now)
        found = versions.regressions(conn, since=since, now=now)
    finally:
        conn.close()
    if not changed:
        print("\nNo application updated in this window.\n")
        return 0
    print("\n%d update%s." % (len(changed), "" if len(changed) == 1 else "s"))
    if found:
        print("\nWhat changed for the worse or better:")
        for item in found:
            unit = "%" if item["metric"] == "cpu" else " MB"
            print("  %-18s %s -> %s   %s %.1f%s -> %.1f%s  (%.1fx %s)"
                  % (item["app"], item["from_version"], item["to_version"],
                     "cpu" if item["metric"] == "cpu" else "mem",
                     item["before"], unit, item["after"], unit,
                     item["ratio"], "worse" if item["worse"] else "better"))
    else:
        print("\nNothing measurably changed for any of them.")
    print()
    for item in changed[:10]:
        print("  %-18s %s -> %-12s %s"
              % (item["app"], item["from_version"], item["to_version"],
                 time.strftime("%d %b", time.localtime(item["changed_ts"]))))
    print()
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
    # How a peer answers: run the same read-only API locally and print it.
    # Not a network service -- it reads stdin-less arguments and writes JSON,
    # so the only way to reach it is to already have shell access.
    fetcher = sub.add_parser("fetch")
    fetcher.add_argument("path")
    fetcher.add_argument("query", nargs="?", default="")

    peerer = sub.add_parser("peer")
    peerer.add_argument("action", choices=["add", "list", "remove", "check"],
                        nargs="?", default="list")
    peerer.add_argument("name", nargs="?")
    peerer.add_argument("host", nargs="?")
    peerer.add_argument("--key", default="",
                        help="the three words that machine printed")

    # The question the whole tool exists to answer, asked the way people ask
    # it. `procwatch why` with no arguments means "just now".
    whyer = sub.add_parser("why")
    whyer.add_argument("--for", dest="span", default="1h",
                       help="how far back: 30m, 2h, 1d")
    whyer.add_argument("--at", default="",
                       help="a time to look at instead, as HH:MM or a unix time")

    # What a process is. The one command here that answers a question people
    # currently take to a search engine and get a forum post from 2013.
    whater = sub.add_parser("what")
    whater.add_argument("name", help="a process name, such as mds_stores")

    # Everything that happened, as opposed to everything that was measured.
    eventer = sub.add_parser("events")
    eventer.add_argument("--for", dest="span", default="30d",
                         help="how far back: 7d, 30d, 1y")
    eventer.add_argument("--all", action="store_true",
                         help="print every event, not only the digest")

    # What happened, and whether to be told about it without looking.
    finder = sub.add_parser("findings")
    finder.add_argument("--for", dest="span", default="1h",
                        help="how far back to look: 30m, 2h, 1d")
    finder.add_argument("--notify", choices=list(prefs.NOTIFY_CHOICES),
                        help="off, causes, or all")
    finder.add_argument("--off", action="store_true",
                        help="stop working out what happened entirely")
    finder.add_argument("--on", action="store_true", help="start again")

    # Where the disk went, and what can be taken back.
    spacer = sub.add_parser("space")
    spacer.add_argument("--scan", action="store_true",
                        help="walk again rather than using the stored scan")
    spacer.add_argument("--root", help="somewhere other than your home folder")
    spacer.add_argument("--under", help="list the folders inside this one")

    cacher = sub.add_parser("caches")
    cacher.add_argument("--clear", action="store_true",
                        help="move them all to the Trash")

    sleeper = sub.add_parser("sleep")
    sleeper.add_argument("--for", dest="span", default="1d")

    grower = sub.add_parser("growth")
    grower.add_argument("--for", dest="span", default="7d")

    updater = sub.add_parser("updates")
    updater.add_argument("--for", dest="span", default="30d")

    keyer = sub.add_parser("key")
    keyer.add_argument("--new", action="store_true",
                       help="forget the old key and make a new one")

    sharer = sub.add_parser("share")
    sharer.add_argument("--port", type=int, default=share.DEFAULT_PORT)
    sharer.add_argument("--new-key", action="store_true",
                        help="forget the old key and print a new one")
    # The key travels as a plain header over plain HTTP, so which interface
    # this listens on is a security decision rather than a detail. It stays
    # open by default -- that is what makes a second Mac on the sofa work --
    # but a machine reachable from the internet wants 127.0.0.1 and an SSH
    # tunnel, and until this existed there was no way to say so.
    sharer.add_argument("--host", default="0.0.0.0",
                        help="interface to listen on; 127.0.0.1 to accept "
                             "only connections tunnelled to this machine")

    builder = sub.add_parser("app")
    builder.add_argument("--to", default="/Applications")
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
    if args.command == "fetch":
        return _fetch(args.path, args.query)
    if args.command == "peer":
        return _peer(args)
    if args.command == "why":
        return _why(args)
    if args.command == "space":
        return _space(args)
    if args.command == "caches":
        return _caches(args)
    if args.command == "findings":
        if args.on and args.off:
            print("--on and --off contradict each other", file=sys.stderr)
            return 1
        return _findings(args)
    if args.command == "events":
        return _events(args)
    if args.command == "what":
        return _what(args)
    if args.command == "sleep":
        return _sleep(args)
    if args.command == "growth":
        return _growth(args)
    if args.command == "updates":
        return _updates(args)
    if args.command == "key":
        conn = db.connect(config.DB_PATH)
        try:
            secret = share.key(conn, reset=args.new)
        finally:
            conn.close()
        print(secret)
        return 0
    if args.command == "share":
        return share.serve(args.port, host=args.host, reset=args.new_key)
    if args.command == "app":
        try:
            path = appbuild.build(args.to)
        except (RuntimeError, OSError) as error:
            print(error, file=sys.stderr)
            return 1
        print("installed %s" % path)
        appbuild.launch(path)
        return 0
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
