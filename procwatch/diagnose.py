"""Say what happened, in a sentence, with the evidence behind it.

Every other tool in this category hands you a chart and leaves the reading to
you. That is the gap this closes. Somebody whose Mac was slow at 2:40pm does
not want a graph of 2:40pm; they want to be told that Spotlight spent nine
minutes reindexing after Xcode wrote twelve gigabytes, that it has finished,
and that there is nothing to do.

Three rules the whole file follows.

Never guess. Every finding carries the numbers it was derived from, so a claim
can be checked and, when it is wrong, seen to be wrong. A confident sentence
with no evidence under it is worse than a chart.

Say when nothing happened. A diagnosis that always finds a culprit is a
horoscope. "Nothing was wrong with your Mac in this window" is a real answer
and often the true one.

Rank by what it cost, not by what is easy to detect. A process at 300% for ten
minutes matters; one at 8% for a day usually does not, whatever the peak says.
"""
import time

from . import alerts, config, events, knowledge, power, prefs, storage, versions

# Below this a process was not the reason for anything a person noticed.
BUSY_CPU = 60.0
# How much of one core a background job has to hold to be worth naming.
CHATTY_CPU = 25.0
# A window has to be at least this long before "sustained" means anything.
MIN_SUSTAINED = 4 * 60

# macOS jobs that are famously the answer, and what they are actually doing.
# Naming these is most of the value: "mds_stores" means nothing to most
# people, and "Spotlight is rebuilding its index" means everything.
SYSTEM_WORK = {
    "mds_stores": ("Spotlight", "rebuilding its search index"),
    "mds": ("Spotlight", "indexing files"),
    "mdworker_shared": ("Spotlight", "reading new files"),
    "backupd": ("Time Machine", "backing up"),
    "photoanalysisd": ("Photos", "analysing your library"),
    "photolibraryd": ("Photos", "working through your library"),
    "cloudd": ("iCloud", "syncing"),
    "bird": ("iCloud Drive", "syncing"),
    "syncdefaultsd": ("iCloud", "syncing settings"),
    "AppleSpell": ("spell checking", "rebuilding its dictionary"),
    "kernel_task": ("macOS", "holding the CPU back to cool the machine"),
    "XProtect": ("XProtect", "scanning for malware"),
    "XprotectService": ("XProtect", "scanning for malware"),
    "softwareupdated": ("Software Update", "downloading an update"),
    "installd": ("macOS", "installing something"),
    "Spotlight": ("Spotlight", "indexing"),
    "corespotlightd": ("Spotlight", "indexing"),
    "AssetCacheLocatorService": ("macOS", "looking for a caching server"),
}


# Holds that are simply how macOS works, and naming them would be the
# horoscope this file is meant not to be.
#
# powerd's is the display-on assertion -- it means somebody is sitting there,
# which is not a fault. cupsd holds a network assertion permanently so printers
# can find the Mac. Reporting either as "something stopped your Mac sleeping"
# is true and worthless, and worthless answers are what teach people to stop
# reading.
BENIGN_HOLDS = {
    ("powerd", "PreventUserIdleSystemSleep"),
    ("cupsd", "NetworkClientActive"),
    ("WindowServer", "PreventUserIdleSystemSleep"),
    ("coreaudiod", "PreventUserIdleSystemSleep"),
}

# The assertion powerd holds while the screen is on. If it was held for most of
# a window then somebody was using the machine, and nothing "kept it awake" --
# it was awake because that is what was wanted.
DISPLAY_ON = ("powerd", "PreventUserIdleSystemSleep")


def _finding(kind, headline, detail, severity, evidence, advice="", when=None,
             about=None):
    """One thing that happened, with its numbers and -- where a process is
    involved -- what that process actually is.

    `about` is the knowledge entry, carried alongside rather than folded into
    the prose so the interface can offer it without lengthening the sentence.
    A finding stays one line; the explanation is there for whoever wants it.
    """
    return {"kind": kind, "headline": headline, "detail": detail,
            "severity": severity, "evidence": evidence, "advice": advice,
            "when": when, "about": about}


def _because(text):
    """The catalogue's "what high usage means" sentence, introduced.

    Introduced rather than pasted in bare, because on its own it reads as a
    claim about this moment ("Usually rebuilding after an update") when it is
    really a statement about the process in general.
    """
    return "When it is busy: %s" % text if text else ""


def _fmt_duration(seconds):
    seconds = int(seconds or 0)
    if seconds >= 7200:
        return "%.1f hours" % (seconds / 3600.0)
    if seconds >= 120:
        return "%d minutes" % (seconds // 60)
    return "%d seconds" % seconds


def _fmt_bytes(count):
    count = float(count or 0)
    for unit, size in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if abs(count) >= size:
            return "%.1f %s" % (count / size, unit)
    return "%d bytes" % count


def _join(names):
    """"a, b and c" -- "a and b and c" reads like a machine wrote it."""
    names = list(names)
    if len(names) < 2:
        return names[0] if names else ""
    return "%s and %s" % (", ".join(names[:-1]), names[-1])


def _clock(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def _series(conn, start, end):
    """Per-application CPU and memory over the window, from the raw tier.

    Grouped by application rather than by process, because the answer people
    can act on is "Chrome", not one of Chrome's thirty renderers.
    """
    rows = conn.execute(
        "SELECT COALESCE(NULLIF(p.app, ''), p.exe) AS name, "
        "       AVG(s.cpu_avg)/10.0, MAX(s.cpu_max)/10.0, "
        "       MAX(s.cpu_max_ts), COUNT(*), AVG(s.rss_avg)/1024.0, "
        "       SUM(s.disk_read + s.disk_write), p.is_system, p.exe "
        "FROM sample_raw s JOIN proc p ON p.id = s.proc_id "
        "WHERE s.ts >= ? AND s.ts < ? AND p.exe != ? "
        "GROUP BY name ORDER BY 2 DESC", (start, end, config.OTHER)).fetchall()
    return [{"name": r[0], "cpu_avg": r[1], "cpu_peak": r[2], "peak_ts": r[3],
             "samples": r[4], "memory_mb": r[5], "disk_bytes": r[6] or 0,
             "is_system": bool(r[7]), "exe": r[8]} for r in rows]


def _system(conn, start, end):
    row = conn.execute(
        "SELECT AVG(cpu_busy)/10.0, MAX(cpu_busy)/10.0, MAX(swap_used_kb), "
        "       MIN(swap_used_kb), AVG(mem_comp_kb), COUNT(*) "
        "FROM system_raw WHERE ts >= ? AND ts < ?", (start, end)).fetchone()
    if not row or not row[5]:
        return None
    return {"cpu_avg": row[0] or 0, "cpu_peak": row[1] or 0,
            "swap_max_kb": row[2] or 0, "swap_min_kb": row[3] or 0,
            "compressed_kb": row[4] or 0, "samples": row[5]}


def _busy_stretch(conn, name, start, end):
    """The longest run of consecutive samples where an application was busy.

    Sustain is what separates a compiler starting up from something wrong, and
    it cannot be read off an average: ten minutes at 300% and a day at 8% can
    average the same.
    """
    rows = conn.execute(
        "SELECT s.ts, SUM(s.cpu_avg)/10.0 FROM sample_raw s "
        "JOIN proc p ON p.id = s.proc_id "
        "WHERE COALESCE(NULLIF(p.app, ''), p.exe) = ? AND s.ts >= ? AND s.ts < ? "
        "GROUP BY s.ts ORDER BY s.ts", (name, start, end)).fetchall()
    best = current = None
    for ts, cpu in rows:
        if cpu >= CHATTY_CPU:
            if current is None:
                current = [ts, ts, cpu, 1]
            else:
                # A gap wider than a few samples is a new stretch, not a
                # continuation -- the machine may have been asleep between.
                if ts - current[1] > config.INTERVAL * 3:
                    current = [ts, ts, cpu, 1]
                else:
                    current[1] = ts
                    current[2] = max(current[2], cpu)
                    current[3] += 1
            if best is None or (current[1] - current[0]) > (best[1] - best[0]):
                best = list(current)
        else:
            current = None
    if not best:
        return None
    return {"start": best[0], "end": best[1] + config.INTERVAL,
            "seconds": best[1] - best[0] + config.INTERVAL,
            "peak": best[2], "samples": best[3]}


def explain(conn, start=None, end=None, now=None):
    """Findings for a window, worst first.

    An empty list is a real answer and means nothing was wrong.
    """
    now = int(time.time()) if now is None else now
    end = now if end is None else end
    start = (end - 3600) if start is None else start

    system = _system(conn, start, end)
    if system is None:
        return {"start": start, "end": end, "findings": [],
                "verdict": "Nothing was recorded in this window."}

    apps = _series(conn, start, end)
    findings = []

    for app in apps:
        if app["cpu_avg"] < 1.0 and app["cpu_peak"] < BUSY_CPU:
            continue
        stretch = _busy_stretch(conn, app["name"], start, end)
        if not stretch:
            continue

        friendly, doing = SYSTEM_WORK.get(app["exe"], (None, None))
        sustained = stretch["seconds"] >= MIN_SUSTAINED
        finished = (end - stretch["end"]) > config.INTERVAL * 4
        about = knowledge.describe(app["exe"], app=app["name"])

        if friendly:
            # A named macOS job. These are the most common true answer and the
            # least useful raw: nobody knows what mds_stores is.
            findings.append(_finding(
                "system-work",
                "%s was %s" % (friendly, doing),
                "%s held %.0f%% of a core for %s%s. This is macOS doing "
                "housekeeping, not something you started."
                % (app["exe"], stretch["peak"], _fmt_duration(stretch["seconds"]),
                   ", and it has finished" if finished else ", and it is still going"),
                "cost" if sustained else "note",
                {"process": app["exe"], "peak_cpu": round(stretch["peak"], 1),
                 "seconds": stretch["seconds"], "from": stretch["start"],
                 "to": stretch["end"], "samples": stretch["samples"]},
                "Nothing to do. It stops on its own." if finished else
                "Leave it plugged in and it will finish sooner.",
                stretch["start"], about))
        elif about["known"] and about["cat"] == knowledge.APPLE:
            # A part of macOS the catalogue knows, which the SYSTEM_WORK list
            # above does not have a headline phrase for. Before this existed,
            # WindowServer or cfprefsd or hidd came out as "an app was working
            # hard" with advice to quit it -- advice that is impossible to take
            # and wrong to give. Naming it as macOS, saying what it does, and
            # saying whether being busy is expected is the whole difference
            # between a monitor and an answer.
            findings.append(_finding(
                "system-known",
                "%s was busy (%s)" % (about["name"], app["exe"]),
                # The measurement only. What the process is, whether being busy
                # is expected and what to do about it all travel in `about`,
                # where the interface can offer them behind an icon -- a finding
                # that opens with two sentences of encyclopedia buries the one
                # sentence that is about this machine at this time.
                "It held up to %.0f%% of a core for %s, from %s%s."
                % (stretch["peak"], _fmt_duration(stretch["seconds"]),
                   _clock(stretch["start"]),
                   ", and it has stopped" if finished else
                   ", and it is still going"),
                "cost" if sustained and stretch["peak"] >= BUSY_CPU else "note",
                {"process": app["exe"], "peak_cpu": round(stretch["peak"], 1),
                 "average_cpu": round(app["cpu_avg"], 1),
                 "seconds": stretch["seconds"], "from": stretch["start"],
                 "to": stretch["end"], "samples": stretch["samples"]},
                about["advice"], stretch["start"], about))
        elif sustained and stretch["peak"] >= BUSY_CPU:
            findings.append(_finding(
                "busy-app",
                "%s was working hard" % app["name"],
                "It held up to %.0f%% of a core for %s, from %s. That is "
                "enough to make the rest of the machine feel slow."
                % (stretch["peak"], _fmt_duration(stretch["seconds"]),
                   _clock(stretch["start"])),
                "cause" if stretch["peak"] >= 150 else "cost",
                {"application": app["name"], "peak_cpu": round(stretch["peak"], 1),
                 "average_cpu": round(app["cpu_avg"], 1),
                 "seconds": stretch["seconds"], "from": stretch["start"],
                 "to": stretch["end"]},
                # Only a catalogue entry earns the right to replace this.
                # A shape guess does not: these findings are grouped by
                # application, so the process behind "Arc" may be one of its
                # renderers, and "look at the application it belongs to" is
                # nonsense advice to give about the application itself.
                (about["advice"] if about["known"] else "") or
                ("It has stopped." if finished else
                 "Quit and reopen it if you are not using it for anything."),
                stretch["start"], about))

    # Memory pressure. Swap growing is the difference between "a lot of memory
    # is in use" -- which is normal and fine -- and the machine actually
    # struggling.
    swap_grew = system["swap_max_kb"] - system["swap_min_kb"]
    # Scaled to the window. Swap creeping up over a day is ordinary; half a
    # gigabyte in ten minutes is the machine struggling. A fixed threshold
    # reports every long window as a memory problem.
    hours = max((end - start) / 3600.0, 0.25)
    swap_floor = max(512 * 1024, int(256 * 1024 * hours))
    if swap_grew > swap_floor:
        heaviest = sorted(apps, key=lambda a: -a["memory_mb"])[:3]
        findings.append(_finding(
            "memory-pressure",
            "Your Mac ran out of memory and started using the disk",
            "Swap grew by %s in this window. The largest were %s. Using the "
            "disk as memory is many times slower, and it is the usual reason a "
            "Mac feels sluggish rather than busy."
            % (_fmt_bytes(swap_grew * 1024),
               ", ".join("%s (%s)" % (a["name"], _fmt_bytes(a["memory_mb"] * 1e6))
                         for a in heaviest if a["memory_mb"] > 200) or "nothing large"),
            "cause",
            {"swap_growth_bytes": swap_grew * 1024,
             "swap_peak_bytes": system["swap_max_kb"] * 1024,
             "largest": [{"name": a["name"], "memory_mb": round(a["memory_mb"])}
                         for a in heaviest]},
            "Quit whatever you are not using, biggest first."))

    # Disk thrash, which reads as a slow machine even when the CPU is idle.
    for app in sorted(apps, key=lambda a: -a["disk_bytes"])[:1]:
        window = max(end - start, 1)
        rate = app["disk_bytes"] / float(window)
        if rate > 20e6:
            findings.append(_finding(
                "disk-thrash",
                "%s was hammering the disk" % app["name"],
                "It read and wrote %s in this window, an average of %s a "
                "second. Everything else waits behind that."
                % (_fmt_bytes(app["disk_bytes"]), _fmt_bytes(rate)),
                "cost",
                {"application": app["name"], "bytes": int(app["disk_bytes"]),
                 "bytes_per_second": int(rate)}))

    findings.extend(_event_findings(conn, start, end, now))
    findings.extend(_power_findings(conn, start, end, now))
    findings.extend(_update_findings(conn, start, end, now))
    findings.extend(_disk_findings(conn, start, end, now))

    findings = _merge_system_work(findings)

    order = {"cause": 0, "cost": 1, "note": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 3),
                                 -(f["evidence"].get("seconds") or 0)))
    return {"start": start, "end": end, "findings": findings,
            "verdict": _verdict(findings, system)}


def _merge_system_work(findings):
    """One finding per piece of macOS housekeeping, not one per process.

    Spotlight is mds_stores, mds and mdworker_shared; Photos is two more.
    Reporting each separately says the same thing three times and pushes
    whatever actually mattered off the screen.
    """
    merged, out = {}, []
    for finding in findings:
        if finding["kind"] != "system-work":
            out.append(finding)
            continue
        # The headline names the thing ("Spotlight was ..."), so it is the
        # natural key -- two processes doing different work stay separate.
        name = finding["headline"].split(" was ")[0]
        first = merged.get(name)
        if first is None:
            finding["evidence"]["processes"] = [finding["evidence"]["process"]]
            merged[name] = finding
            out.append(finding)
            continue
        first["evidence"]["processes"].append(finding["evidence"]["process"])
        first["evidence"]["seconds"] = max(first["evidence"]["seconds"],
                                           finding["evidence"]["seconds"])
        first["evidence"]["peak_cpu"] = max(first["evidence"]["peak_cpu"],
                                            finding["evidence"]["peak_cpu"])
        if finding["severity"] == "cost":
            first["severity"] = "cost"
        first["detail"] = (
            "%s held up to %.0f%% of a core for %s. This is macOS doing "
            "housekeeping, not something you started."
            % (_join(first["evidence"]["processes"]),
               first["evidence"]["peak_cpu"],
               _fmt_duration(first["evidence"]["seconds"])))
    return out


def _event_findings(conn, start, end, now):
    """What happened, as opposed to what was measured.

    The samplers can say a process was busy; only the event history can say it
    crashed, that the machine restarted without being shut down, or that macOS
    filed its ninth excess-CPU report about the same daemon this week. None of
    that is visible in a chart, and all of it answers "why".
    """
    out = []
    try:
        faults = events.timeline(conn, start, end, limit=60, context=False)
    except Exception:
        # The event tables may not exist yet on a database written by an older
        # build. A missing history is a missing finding, not a failed verdict.
        return out

    # Things that actually broke, newest first, with what the process is.
    for row in faults:
        if row["kind"] not in ("panic", "unclean-shutdown", "shutdown-stall",
                               "crash", "hang", "spin"):
            continue
        about = row.get("about") or {}
        detail = row["meaning"]
        if about.get("known"):
            detail = "%s %s" % (detail, about["does"])
        out.append(_finding(
            "event-" + row["kind"], row["headline"],
            "%s At %s." % (detail, _clock(row["ts"])),
            "cause",
            {"kind": row["kind"], "subject": row["subject"], "at": row["ts"],
             "source": row["source"]},
            about.get("advice", ""), row["ts"], row.get("about")))

    # A repeat is worth more than any single occurrence, and it is the one thing
    # no chart and no crash report can show: they each see one moment.
    look_back = max(14 * 86400, end - start)
    for pattern in events.patterns(conn, end - look_back, end)[:3]:
        if pattern["last_ts"] < start and not pattern["burst"]:
            continue
        if pattern["count"] < events.MIN_REPEATS:
            continue
        about = pattern.get("about") or {}
        out.append(_finding(
            "event-repeat",
            "%s, and not for the first time" % pattern["headline"],
            "%s %s" % (events.describe_pattern(pattern, now),
                       about.get("does", "")),
            "cost" if pattern["burst"] else "note",
            {"kind": pattern["kind"], "subject": pattern["subject"],
             "count": pattern["count"], "first": pattern["first_ts"],
             "last": pattern["last_ts"], "burst": pattern["burst"]},
            about.get("advice", ""), pattern["last_ts"], pattern.get("about")))

    # An OS update inside the window explains almost everything that follows it.
    for row in faults:
        if row["kind"] != "os-update":
            continue
        out.append(_finding(
            "event-os-update", "macOS was updated",
            "%s was installed at %s. %s"
            % (row["subject"], _clock(row["ts"]),
               events.MEANINGS["os-update"][1]),
            "note", {"version": row["subject"], "at": row["ts"]},
            "Nothing. Leave it plugged in and it will settle.", row["ts"]))
    return out


def _power_findings(conn, start, end, now):
    """What kept the machine awake, and what the lid being shut cost."""
    out = []
    try:
        held = power.kept_awake(conn, start, end, limit=3)
        drain = power.overnight_drain(conn, start, end)
        spans = power.nights(conn, start, end)
    except Exception:
        return out

    window = max(end - start, 1)

    # Was somebody using the machine? If the display was on for most of the
    # window then it was awake because that was wanted, and nothing "kept it
    # awake". This one check is what stops the whole feature crying wolf.
    display_on = 0
    for hold in power.kept_awake(conn, start, end, limit=40):
        if hold["process"] == DISPLAY_ON[0] and DISPLAY_ON[1] in hold["kinds"]:
            display_on = max(display_on, hold["seconds"])
    in_use = display_on > window * 0.5

    blamed = [h for h in held
              if h["seconds"] >= max(1800, window * 0.5)
              and not all((h["process"], k) in BENIGN_HOLDS for k in h["kinds"])]
    if blamed and not in_use:
        # One finding naming them, not one each: three near-identical entries
        # push everything else off the screen and say the same thing.
        names = ", ".join(h["process"] for h in blamed[:3])
        longest = blamed[0]
        out.append(_finding(
            "kept-awake",
            "%s stopped your Mac sleeping" % names,
            "%s held a sleep assertion for %s of a window when nobody was "
            "using the machine. While one is held it cannot sleep, so it keeps "
            "working and keeps using the battery."
            % (names, _fmt_duration(longest["seconds"])),
            "cause",
            {"processes": [{"process": h["process"], "seconds": h["seconds"],
                            "kinds": h["kinds"], "names": h["names"]}
                           for h in blamed[:3]],
             "seconds": longest["seconds"], "display_on_seconds": display_on},
            "Quit them before you close the lid, or leave it plugged in."))
    elif blamed and in_use:
        out.append(_finding(
            "kept-awake",
            "%s is holding your Mac awake" % blamed[0]["process"],
            "It has held a sleep assertion for %s. That is fine while you are "
            "using the machine, but it will also stop it sleeping when you "
            "shut the lid."
            % _fmt_duration(blamed[0]["seconds"]),
            "note",
            {"processes": [{"process": h["process"], "seconds": h["seconds"],
                            "kinds": h["kinds"]} for h in blamed[:3]],
             "display_on_seconds": display_on},
            "Worth quitting before you put it in a bag."))

    if drain and drain["charge_lost"] >= 8:
        reasons = {}
        for span in spans:
            if span.get("woke_because"):
                reasons[span["woke_because"]] = reasons.get(span["woke_because"], 0) + 1
        top = sorted(reasons.items(), key=lambda kv: -kv[1])[:2]
        out.append(_finding(
            "sleep-drain",
            "Your Mac lost %d%% of its battery while asleep" % drain["charge_lost"],
            "It was asleep for %s and woke %d times, mostly because of %s. "
            "Each wake costs a little charge."
            % (_fmt_duration(drain["asleep_seconds"]), drain["wakes"],
               " and ".join(name for name, _ in top) or "unknown causes"),
            "cause" if drain["charge_lost"] >= 15 else "cost",
            {"charge_lost": drain["charge_lost"],
             "asleep_seconds": drain["asleep_seconds"],
             "wakes": drain["wakes"], "dark_wakes": drain["dark_wakes"],
             "per_hour": round(drain["per_hour"], 2) if drain["per_hour"] else None,
             "causes": dict(top)},
            "Turn off Wake for network access in Battery settings if this "
            "matters to you."))
    return out


def _update_findings(conn, start, end, now):
    out = []
    try:
        found = versions.regressions(conn, since=start - 14 * 86400, now=now)
    except Exception:
        return out
    for item in found[:3]:
        if not item["worse"]:
            continue
        unit = "%" if item["metric"] == "cpu" else " MB"
        out.append(_finding(
            "update-regression",
            "%s got heavier when it updated" % item["app"],
            "Since %s replaced %s, its %s went from %.1f%s to %.1f%s — %.1f "
            "times as much. Nothing you did caused this."
            % (item["to_version"], item["from_version"],
               "CPU" if item["metric"] == "cpu" else "memory",
               item["before"], unit, item["after"], unit, item["ratio"]),
            "cost",
            {"application": item["app"], "metric": item["metric"],
             "from_version": item["from_version"],
             "to_version": item["to_version"], "before": item["before"],
             "after": item["after"], "ratio": item["ratio"],
             "changed_ts": item["changed_ts"], "samples": item["samples"]},
            "Worth reporting to whoever makes it."))
    return out


def _disk_findings(conn, start, end, now):
    out = []
    try:
        report = storage.growth(conn, since=start - 7 * 86400, now=now)
    except Exception:
        return out
    if report["days_compared"] < 2:
        return out
    grew = [a for a in report["apps"] if a["change"] > 1e9][:3]
    if not grew:
        return out
    out.append(_finding(
        "disk-growth",
        "You have %s less free space than a week ago" % _fmt_bytes(
            abs(report["total_change"])) if report["total_change"] > 0
        else "Your disk usage fell by %s this week" % _fmt_bytes(
            abs(report["total_change"])),
        "The biggest movers were %s."
        % ", ".join("%s (%s%s)" % (a["app"], "+" if a["change"] > 0 else "",
                                   _fmt_bytes(a["change"])) for a in grew),
        "note",
        {"total_change_bytes": report["total_change"],
         "apps": grew, "from_day": report["from_day"],
         "to_day": report["to_day"]}))
    return out


def _verdict(findings, system):
    """One sentence at the top.

    Explicitly willing to say nothing was wrong. A tool that always finds a
    culprit is a horoscope, and the most common truthful answer about a
    working machine is that it was fine.
    """
    causes = [f for f in findings if f["severity"] == "cause"]
    if causes:
        if len(causes) == 1:
            return causes[0]["headline"] + "."
        return "%s, and %d other thing%s." % (
            causes[0]["headline"], len(causes) - 1,
            "" if len(causes) == 2 else "s")
    costs = [f for f in findings if f["severity"] == "cost"]
    if costs:
        # Not lowercased: the headline usually begins with a name, and
        # "spotlight" and "iCloud" are both wrong when mangled.
        return "Nothing was really wrong, but %s." % costs[0]["headline"]
    if system["cpu_avg"] < 30:
        return "Your Mac was fine in this window. Nothing was holding it up."
    return "Your Mac was busy but nothing stood out as the cause."


# ---------------------------------------------------------------------------
# Telling somebody, once, when something new turns up.
#
# The verdict is computed on demand: you open the dashboard, it explains the
# last hour. That is the right shape for a question somebody asked, and it is
# useless for the thing they actually want -- to be told when their Mac starts
# doing something worth knowing about, without having to go and look.
#
# So the recorder works the verdict out on a slow cadence and posts a
# notification for findings it has not already mentioned. Three things decide
# whether that is welcome or infuriating.
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS finding_seen (
  key       TEXT PRIMARY KEY,
  first_ts  INTEGER NOT NULL,
  last_ts   INTEGER NOT NULL,
  told_ts   INTEGER NOT NULL DEFAULT 0,
  -- When the person last looked at this one. Separate from told_ts, which is
  -- when the machine last mentioned it: being notified and having read it are
  -- different things, and the menu bar count is about the second.
  ack_ts    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS finding_state (
  key   TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);
"""

# The verdict costs a dozen queries plus the power and event tables. Every
# thirty seconds would be waste for something nobody needs to the second.
WATCH_EVERY = 5 * 60

# The window the watcher explains. An hour is what "just now" means, and long
# enough that a finding does not vanish between two passes.
WATCH_SPAN = 3600

# Having mentioned something, stay quiet about it for this long. Spotlight
# reindexing for three hours is one piece of news, not thirty-six.
REARM = 6 * 3600

# Which findings are worth interrupting for, per preference.
NOTIFY_SEVERITIES = {
    "off": (),
    "causes": ("cause",),
    "all": ("cause", "cost"),
}


def watch_init(conn):
    """Make the tables, and add the columns a database from before them lacks.

    Here rather than in db.init_schema because this module owns these tables,
    and because init_schema is not on every path that reaches them -- a
    connection opened by hand still has to find the column it is about to read.
    CREATE TABLE IF NOT EXISTS says nothing about columns.
    """
    with conn:
        conn.executescript(DDL)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(finding_seen)")}
        if columns and "ack_ts" not in columns:
            conn.execute("ALTER TABLE finding_seen ADD COLUMN "
                         "ack_ts INTEGER NOT NULL DEFAULT 0")


def _finding_key(finding):
    """What makes two findings the same finding.

    The kind plus whatever it is about -- not the headline, which carries
    durations and clock times and would therefore make every pass a new
    finding, and not the evidence, which carries the measurements themselves.
    """
    evidence = finding.get("evidence") or {}
    for field in ("process", "application", "subject", "version", "kind"):
        value = evidence.get(field)
        if value:
            return "%s:%s" % (finding["kind"], value)
    return finding["kind"]


def watch_due(conn, now=None, every=WATCH_EVERY):
    now = int(time.time()) if now is None else now
    try:
        row = conn.execute(
            "SELECT value FROM finding_state WHERE key='watched'").fetchone()
    except Exception:
        return True
    return not row or (now - row[0]) >= every


def watch(conn, now=None, span=WATCH_SPAN, post=None):
    """Work out what happened, and mention anything new. Returns what it said.

    Called by the recorder, not by the dashboard: the point is to be told
    without having to look.
    """
    now = int(time.time()) if now is None else now
    watch_init(conn)
    prefs.init(conn)
    told = []
    if not prefs.findings_on(conn):
        # Off means off: no diagnosis, no bookkeeping, no cost. Deliberately
        # before the timestamp is written, so turning it back on does not have
        # to wait out an interval.
        return told

    wanted = NOTIFY_SEVERITIES.get(prefs.get(conn, "findings_notify"), ())
    result = explain(conn, now - span, now, now=now)

    # Whether anything has ever been recorded here. The first pass after
    # installing -- or after switching this back on -- must not empty a backlog
    # onto the screen: everything true at that moment is new to the table and
    # none of it is news to the person, who has been using the machine all
    # along. So the first pass learns the findings silently.
    known = conn.execute("SELECT COUNT(*) FROM finding_seen").fetchone()[0]
    quiet = known == 0

    with conn:
        for finding in result["findings"]:
            key = _finding_key(finding)
            row = conn.execute(
                "SELECT told_ts FROM finding_seen WHERE key = ?",
                (key,)).fetchone()
            fresh = row is None or (now - row[0]) >= REARM
            say = (not quiet and fresh
                   and finding["severity"] in wanted)
            conn.execute(
                "INSERT INTO finding_seen (key, first_ts, last_ts, told_ts) "
                "VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "last_ts=excluded.last_ts, "
                "told_ts=CASE WHEN ? THEN excluded.told_ts ELSE told_ts END",
                (key, now, now, now if say else 0, 1 if say else 0))
            if say:
                told.append(finding)
        conn.execute("INSERT OR REPLACE INTO finding_state (key, value) "
                     "VALUES ('watched', ?)", (now,))

    sender = post or alerts.post
    for finding in told:
        # The headline is the sentence; the advice is what to do about it. A
        # notification with only the first is a shrug with a title.
        sender(finding["headline"], finding.get("advice") or finding["detail"])
    return told


def forget_findings(conn):
    """Drop what has been mentioned, so the next pass starts quiet again.

    Called when the diagnosis is switched off. Otherwise switching it on months
    later would compare against a table remembering the last time -- and stay
    silent about things that are genuinely new now.
    """
    watch_init(conn)
    with conn:
        conn.execute("DELETE FROM finding_seen")
        conn.execute("DELETE FROM finding_state WHERE key='watched'")


def unread(conn, now=None, span=WATCH_SPAN):
    """How many of the findings that stand right now have not been looked at.

    What the menu bar counts. Deliberately computed from the current verdict
    rather than from the table alone: a finding that has stopped being true
    should stop being counted, and a table of everything ever seen would only
    ever grow.

    Returns (count, keys). The keys come back so whoever displays the count can
    mark exactly those as read, rather than acknowledging findings that arrived
    while somebody was reading.
    """
    now = int(time.time()) if now is None else now
    watch_init(conn)
    prefs.init(conn)
    if not prefs.findings_on(conn):
        return 0, []
    try:
        result = explain(conn, now - span, now, now=now)
    except Exception:
        return 0, []
    keys = []
    for finding in result["findings"]:
        key = _finding_key(finding)
        row = conn.execute("SELECT ack_ts FROM finding_seen WHERE key = ?",
                           (key,)).fetchone()
        if not row or not row[0]:
            keys.append(key)
    return len(keys), keys


def mark_read(conn, keys=None, now=None):
    """Note that these findings have been looked at.

    With no keys, everything standing right now -- which is what opening the
    verdict means. The badge is then empty until something new turns up, which
    is the only behaviour that makes a count worth glancing at.
    """
    now = int(time.time()) if now is None else now
    watch_init(conn)
    if keys is None:
        _, keys = unread(conn, now=now)
    if not keys:
        return 0
    with conn:
        for key in keys:
            # A finding that has never been recorded can still be read: the
            # verdict is computed on demand and the watcher may not have run.
            conn.execute(
                "INSERT INTO finding_seen (key, first_ts, last_ts, told_ts, "
                "ack_ts) VALUES (?,?,?,0,?) ON CONFLICT(key) DO UPDATE SET "
                "ack_ts=excluded.ack_ts", (key, now, now, now))
    return len(keys)
