"""Where the disk went, and what is safe to take back.

Written after watching a 228 GB volume sit at 91% full with no way to find out
why. The tools macOS ships answer the wrong question: About This Mac gives you
seven coloured segments and no paths, and Finder hides ~/Library, which is
where most of it usually is.

Four decisions shape this file.

One walk answers everything. Spotlight can find files quickly but cannot tell
you how big they are, and asking it for the size of 59,000 videos means a stat
each -- slower than the single traversal that already had them. So there is one
pass that records (path, blocks, extension, owner), and every view here is a
different aggregation of it. Format breakdown, biggest folders, per-application
totals and hidden directories are the same data grouped four ways.

Blocks, not bytes. st_size is what a file claims; st_blocks * 512 is what the
disk gave it. On APFS those differ enormously: a cloned file costs almost
nothing, a sparse disk image claims 64 GB and occupies 8. A space tool that
reports the wrong one sends people deleting the wrong things.

Nothing is deleted. Removals move to the Trash, so a mistake is a drag back
rather than a restore from backup, and the caches offered are a list written by
hand -- "anything named cache" includes things applications cannot rebuild.

It is slow and says so. A full home directory is over a million files here and
takes minutes; the scan runs in the background, reports progress, and the result
is stored so opening the panel again is instant.
"""
import os
import plistlib
import subprocess
import time

# What a file is, from its extension. Not from Spotlight's content types: those
# are richer and cost a lookup per file, and the question people ask is "what is
# all my video" rather than "how much public.mpeg-4".
KINDS = {
    "video": ("mp4 mov m4v avi mkv webm flv wmv mpg mpeg m2ts mts vob ogv "
              "prproj fcpbundle braw r3d"),
    "audio": "mp3 m4a wav aiff aif flac aac ogg opus wma mid midi logicx band",
    "images": ("jpg jpeg png gif heic heif tiff tif bmp webp raw cr2 nef arw "
               "dng psd ai sketch fig xcf svg"),
    "documents": ("pdf doc docx pages xls xlsx numbers ppt pptx key txt rtf md "
                  "epub mobi csv"),
    "archives": "zip tar gz tgz bz2 xz 7z rar dmg pkg iso sit",
    "disk images": "dmg sparsebundle sparseimage img qcow2 vdi vmdk vhd",
    "code": ("py js ts jsx tsx c h cpp hpp m mm swift java kt rb go rs php sh "
             "html css json yaml yml toml xml sql"),
    "data": "db sqlite sqlite3 realm mdb dat bin pack idx",
    "fonts": "ttf otf ttc woff woff2 dfont",
}
_BY_EXT = {}
for _kind, _exts in KINDS.items():
    for _ext in _exts.split():
        _BY_EXT.setdefault(_ext, _kind)

# Directories never worth walking. /System and /Volumes are not the user's to
# manage, and the firmlinked copies of /Users under /System/Volumes/Data would
# make everything count twice.
SKIP = (
    "/System", "/Volumes", "/private/var/vm", "/dev", "/net", "/home",
    "/.Spotlight-V100", "/.fseventsd", "/.DocumentRevisions-V100",
)

# Somewhere to stop before the walk becomes its own problem. A million files is
# a few minutes; ten million is not a diagnosis, it is a hang.
MAX_FILES = 4_000_000

# Walked as well as your home folder, because otherwise the totals do not add
# up and a space tool whose numbers do not add up is worth nothing. On the
# machine this was written on these hold 49 GB against 224 GB used, and
# /Applications alone is 25 GB of it -- which is the most actionable number on
# the whole page, since an application is a thing you can drag to the Bin.
#
# /System is deliberately absent: it is the sealed read-only volume, identical
# on every Mac, and nothing anybody can act on.
SYSTEM_ROOTS = ("/Applications", "/Library", "/opt", "/usr/local", "/private/var")

# Places macOS refuses to let an ordinary process read at all. Being refused
# looks exactly like an empty folder, so they are named: a Photos library is
# frequently the largest thing on a Mac, and reporting it as nothing is worse
# than not reporting it.
GUARDED = (
    "~/Pictures/Photos Library.photoslibrary",
    "~/Library/Mail",
    "~/Library/Messages",
    "~/Library/Safari",
    "~/Library/Application Support/AddressBook",
    "~/Library/Application Support/CallHistoryDB",
)

DDL = """
CREATE TABLE IF NOT EXISTS space_scan (
  id          INTEGER PRIMARY KEY,
  root        TEXT NOT NULL,
  roots       TEXT NOT NULL DEFAULT '',
  started_ts  INTEGER NOT NULL,
  finished_ts INTEGER NOT NULL DEFAULT 0,
  files       INTEGER NOT NULL DEFAULT 0,
  dirs        INTEGER NOT NULL DEFAULT 0,
  bytes       INTEGER NOT NULL DEFAULT 0,
  logical     INTEGER NOT NULL DEFAULT 0,
  unreadable  INTEGER NOT NULL DEFAULT 0,
  stopped     TEXT NOT NULL DEFAULT ''
);

-- One row per directory worth naming. Everything smaller than FLOOR is left to
-- its parent's total: a million rows for a million directories answers no
-- question anybody asks.
CREATE TABLE IF NOT EXISTS space_dir (
  scan_id  INTEGER NOT NULL,
  path     TEXT NOT NULL,
  depth    INTEGER NOT NULL,
  bytes    INTEGER NOT NULL,
  files    INTEGER NOT NULL,
  owner    TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (scan_id, path)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS space_file (
  scan_id  INTEGER NOT NULL,
  path     TEXT NOT NULL,
  bytes    INTEGER NOT NULL,
  logical  INTEGER NOT NULL,
  kind     TEXT NOT NULL DEFAULT '',
  owner    TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (scan_id, path)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS space_kind (
  scan_id  INTEGER NOT NULL,
  kind     TEXT NOT NULL,
  bytes    INTEGER NOT NULL,
  files    INTEGER NOT NULL,
  PRIMARY KEY (scan_id, kind)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS space_owner (
  scan_id  INTEGER NOT NULL,
  owner    TEXT NOT NULL,
  bytes    INTEGER NOT NULL,
  files    INTEGER NOT NULL,
  PRIMARY KEY (scan_id, owner)
) WITHOUT ROWID;
"""

# A directory has to be at least this big to be worth a row of its own.
def files_in(path, limit=5000):
    """The files directly inside one folder, read now rather than recalled.

    The recorded scan keeps only files over FILE_FLOOR, because a row per file
    across a whole disk is millions of rows for a question nobody asks. That
    floor is right for the history and wrong for standing in a folder and
    asking what is in it: ~/Downloads/music is 4.3 GB of FLACs at about 20 MB
    each, so every one of them is below the floor and the folder reads as
    empty while holding four gigabytes.

    One directory, not recursive, so it costs a single readdir no matter how
    large the disk is. Sizes are the blocks actually used rather than the
    apparent length, so a sparse file is not reported as the space it is not
    occupying.
    """
    try:
        entries = list(os.scandir(os.path.expanduser(path)))
    except OSError:
        return []
    out = []
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                continue
            stat = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        out.append({"path": entry.path,
                    "bytes": stat.st_blocks * 512 if hasattr(stat, "st_blocks")
                             else stat.st_size,
                    "logical": stat.st_size,
                    "kind": _kind_of(entry.name),
                    "owner": ""})
    out.sort(key=lambda f: -f["bytes"])
    # The cap is a guard against a folder with a million entries, not a view
    # decision -- the page pages through what it gets. The count of what was
    # dropped travels with the list so the interface can say so rather than
    # quietly ending.
    kept = out[:limit]
    if len(out) > limit:
        kept[-1] = dict(kept[-1], truncated=len(out) - limit)
    return kept


FLOOR = 20 * 1024 * 1024
# And a file this big to be worth naming individually.
FILE_FLOOR = 50 * 1024 * 1024


def init(conn):
    with conn:
        conn.executescript(DDL)
        # Added after the table shipped. CREATE TABLE IF NOT EXISTS says
        # nothing about columns.
        columns = {r[1] for r in conn.execute("PRAGMA table_info(space_scan)")}
        if columns and "roots" not in columns:
            conn.execute("ALTER TABLE space_scan ADD COLUMN roots TEXT NOT "
                         "NULL DEFAULT ''")


def guarded():
    """The folders macOS will not let this read, with what is in them.

    Named rather than counted, because the count is what cannot be known: being
    refused is indistinguishable from an empty folder, so these would silently
    weigh nothing. Full Disk Access in System Settings is what changes it.
    """
    out = []
    for path in GUARDED:
        full = os.path.expanduser(path)
        if not os.path.exists(full):
            continue
        try:
            os.listdir(full)
        except PermissionError:
            out.append(path)
        except OSError:
            continue
    return out


def _run(argv, timeout=20):
    try:
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout or ""


_CONTAINER = {"at": 0.0, "for": None, "was": None}


def container(expect=0):
    """The APFS container the data volume lives in, volume by volume.

    statvfs cannot answer this. On APFS every volume in a container shares one
    pool of free space, so statvfs("/System/Volumes/Data") reports the whole
    container's usage against the data volume: 228 GB here, when the data
    volume itself holds 198 and the other 30 are System, Preboot, Recovery and
    VM. Charging the user for those, and then calling the difference between
    them and a scan "missing", is how a third of a disk went unexplained.

    `expect` is the size statvfs reports for the volume, and it is how the
    right container is picked. Taking the first one holding a volume with the
    Data role is wrong on any Mac with a second APFS disk attached: an external
    with its own Data volume was chosen, its usage was reported as the user's,
    and the whole reconcile block silently disappeared because the difference
    came out at zero. Sizes are what tells them apart, so the container has to
    be the size the volume said it was.

    Returns None when diskutil is unavailable or says something unexpected;
    the caller falls back to statvfs, which is wrong in a known direction
    rather than absent.
    """
    # Cached for a few seconds. One page load asks twice -- once for the bar,
    # once for the reconcile note -- and the panel polls every second and a
    # half while a scan runs, which is a process spawned per poll for a number
    # that changes when a volume is added.
    now = time.time()
    if _CONTAINER["at"] > now - 10 and _CONTAINER["for"] == expect:
        return _CONTAINER["was"]
    text = _run(["diskutil", "apfs", "list", "-plist"], timeout=30)
    if not text:
        # Remembered too. The case worth caching most is the one where there is
        # no diskutil to ask: without this, the machine that can never answer
        # is the machine that spawns a process per poll to find that out again.
        _CONTAINER.update({"at": now, "for": expect, "was": None})
        return None
    try:
        parsed = plistlib.loads(text.encode("utf-8", "replace"))
    except Exception:
        _CONTAINER.update({"at": now, "for": expect, "was": None})
        return None
    for box in parsed.get("Containers") or []:
        if expect and int(box.get("CapacityCeiling") or 0) != expect:
            continue
        vols = []
        mine = None
        for vol in box.get("Volumes") or []:
            entry = {"name": vol.get("Name") or "",
                     "role": (vol.get("Roles") or [""])[0] or "",
                     "used": int(vol.get("CapacityInUse") or 0),
                     "mount": vol.get("MountPoint") or ""}
            vols.append(entry)
            # By role, not by mount point: diskutil reports no mount point for
            # the volumes in this list, and the data volume is the one macOS
            # calls Data whatever it is called or where it is mounted.
            if entry["role"] == "Data":
                mine = entry
        if not mine:
            continue
        total = int(box.get("CapacityCeiling") or 0)
        free = int(box.get("CapacityFree") or 0)
        answer = {"total": total, "free": free, "used": max(0, total - free),
                  "volumes": vols, "data": mine,
                  "others": [v for v in vols if v is not mine]}
        _CONTAINER.update({"at": now, "for": expect, "was": answer})
        return answer
    _CONTAINER.update({"at": now, "for": expect, "was": None})
    return None


def volumes():
    """How much space there actually is, and how much of it is yours.

    Not `df /`. On a modern Mac / is a sealed, read-only system snapshot -- it
    reports 12 GB used of 228 and is the same on every Mac. Everything that
    belongs to you is on the data volume, and that is the number worth showing.

    `used` is that volume's own consumption. The container's total usage is
    reported separately as `container_used`, with the volumes making up the
    difference named, because "your files" and "everything on the disk" are
    different questions and only one of them is actionable.
    """
    out = []
    for mount in ("/System/Volumes/Data", "/"):
        try:
            st = os.statvfs(mount)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        out.append({"mount": mount, "total": total, "free": free,
                    "used": total - free,
                    "percent": round((total - free) / total * 100, 1) if total else 0})
    data = [v for v in out if v["mount"] == "/System/Volumes/Data"]
    data = data[0] if data else (out[0] if out else None)
    box = container(data["total"] if data else 0)
    if data and box and box["data"]["used"]:
        data = dict(data)
        data["used"] = box["data"]["used"]
        data["container_used"] = box["used"]
        data["container_free"] = box["free"]
        data["percent"] = (round(data["used"] / data["total"] * 100, 1)
                           if data["total"] else 0)
        data["others"] = [v for v in box["others"] if v["used"]]
    return {"volumes": out, "data": data}


def snapshots():
    """Time Machine's local snapshots, which hold real space and are invisible.

    Finder counts them as used and offers no way to see them; they are the
    usual answer to "the sizes do not add up to the missing space". macOS thins
    them under pressure, so they are reported rather than deleted here.
    """
    text = _run(["tmutil", "listlocalsnapshots", "/"])
    names = [line.strip() for line in text.splitlines()
             if line.strip().startswith("com.apple.")]
    return names


def _apps():
    """Bundle identifier to application name, for attributing containers.

    ~/Library/Containers is named by bundle id, which is how a folder called
    com.hnc.Discord ends up being nobody's fault. Read once per scan.
    """
    found = {}
    for root in ("/Applications", "/Applications/Utilities",
                 os.path.expanduser("~/Applications"),
                 "/System/Applications"):
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".app"):
                continue
            plist = os.path.join(root, name, "Contents", "Info.plist")
            try:
                with open(plist, "rb") as handle:
                    info = plistlib.load(handle)
            except Exception:
                continue
            ident = info.get("CFBundleIdentifier")
            if ident:
                found[ident] = name[:-4]
    return found


class _Owner(object):
    """Which application a path belongs to.

    Path first, because on macOS the path usually says so outright: everything
    under ~/Library/Containers/<bundle id> belongs to that bundle, and
    ~/Library/Application Support/<Name> to that name. Where the path does not
    say, nothing is claimed -- guessing an owner is worse than admitting the
    file is loose in your home directory.
    """

    def __init__(self, home, apps):
        self.home = home
        self.apps = apps
        self.rules = [
            (os.path.join(home, "Library/Containers"), "bundle"),
            (os.path.join(home, "Library/Group Containers"), "group"),
            (os.path.join(home, "Library/Application Support"), "name"),
            (os.path.join(home, "Library/Caches"), "bundle"),
            (os.path.join(home, "Library/Saved Application State"), "bundle"),
            (os.path.join(home, "Library/HTTPStorages"), "bundle"),
            (os.path.join(home, "Library/WebKit"), "bundle"),
            # Longest first: /Applications on its own turned
            # /Applications/Utilities/Terminal.app into an application called
            # "Utilities" that every Utility then belonged to. And an
            # application in ~/Applications had no rule at all, so it owned
            # nothing and always reported zero left behind.
            ("/Applications/Utilities", "app"),
            (os.path.join(home, "Applications"), "app"),
            ("/Applications", "app"),
        ]

    def of(self, path):
        for prefix, how in self.rules:
            if not path.startswith(prefix + os.sep):
                continue
            part = path[len(prefix) + 1:].split(os.sep)[0]
            if how == "app":
                return part[:-4] if part.endswith(".app") else part
            if how == "group":
                # group.com.apple.notes -> com.apple.notes
                ident = part.split("group.", 1)[-1]
                return self.apps.get(ident, ident)
            return self.apps.get(part, part)
        return ""


def _prune(roots):
    """Drop any root that lives inside another one.

    Two roots where one contains the other means the inner one is walked twice:
    once on its own and once on the way down through the outer. Both totals are
    then wrong -- the inner one doubles, and every folder above it is credited
    twice over. Found by giving the scan a deliberately nested pair, where an
    8 KB file came back as 16 KB.

    Nothing in the shipped list nests. This is here because the list is a
    constant somebody will add to, and /usr beside /usr/local is the obvious
    next entry.
    """
    tidy = []
    for r in roots:
        r = os.path.abspath(r).rstrip(os.sep) or os.sep
        if r not in tidy:
            tidy.append(r)
    # Containment is decided shortest-first, so an outer root is always seen
    # before anything it contains. The answer is returned in the caller's own
    # order, which keeps the home folder first -- it is the one the scan is
    # recorded against and the one a view defaults to.
    keep = []
    for candidate in sorted(tidy, key=len):
        if any(candidate.startswith(kept + os.sep) for kept in keep):
            continue
        keep.append(candidate)
    return [r for r in tidy if r in keep]


def _kind_of(name):
    dot = name.rfind(".")
    if dot <= 0:
        return "other"
    return _BY_EXT.get(name[dot + 1:].lower(), "other")


def scan(conn, root=None, progress=None, max_files=MAX_FILES, deadline=None,
         floor=FLOOR, file_floor=FILE_FLOOR):
    """Walk once and record what is there. Returns the scan id.

    `progress` is called with (files, bytes) every so often, so a caller can
    show something moving during the minutes this takes.

    The two floors decide what is worth a row. They default to twenty and fifty
    megabytes, which is right for a home directory and wrong for a small root:
    scanning one folder with a twenty megabyte floor reports nothing at all.
    """
    init(conn)
    home = os.path.expanduser("~")
    if root:
        roots = [os.path.abspath(os.path.expanduser(root))]
    else:
        # Home, and the readable places outside it. Walking only home reported
        # 100 GB against 224 GB used, and a total that does not reconcile is
        # worse than no total: it sends people hunting for space that was never
        # missing.
        roots = [home] + [r for r in SYSTEM_ROOTS if os.path.isdir(r)]
    roots = _prune(roots)
    root = roots[0]
    owner_of = _Owner(home, _apps()).of
    started = int(time.time())

    with conn:
        cur = conn.execute(
            "INSERT INTO space_scan (root, roots, started_ts) VALUES (?,?,?)",
            (root, "\n".join(roots), started))
        scan_id = cur.lastrowid

    dir_bytes = {}
    dir_files = {}
    kinds = {}
    owners = {}
    big_files = []
    files = dirs = unreadable = 0
    total = logical_total = 0
    # (device, inode) of every file seen that has more than one name.
    links = set()
    stopped = ""
    stack = list(roots)
    last_report = time.time()

    while stack:
        here = stack.pop()
        here_bytes = 0
        here_files = 0
        try:
            with os.scandir(here) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.path in SKIP or entry.path.startswith("/Volumes"):
                                continue
                            dirs += 1
                            stack.append(entry.path)
                            continue
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        unreadable += 1
                        continue
                    # What the disk actually gave it. st_size is what the file
                    # claims, and for a sparse image or an APFS clone the two
                    # are nothing alike.
                    #
                    # A file with several names is charged to the first one
                    # reached and to nothing after it, so a tree of hard links
                    # does not report space the disk never gave out.
                    on_disk = st.st_blocks * 512 if counted_once(links, st) else 0
                    files += 1
                    here_files += 1
                    here_bytes += on_disk
                    total += on_disk
                    logical_total += st.st_size

                    kind = _kind_of(entry.name)
                    slot = kinds.setdefault(kind, [0, 0])
                    slot[0] += on_disk
                    slot[1] += 1

                    who = owner_of(entry.path)
                    if who:
                        slot = owners.setdefault(who, [0, 0])
                        slot[0] += on_disk
                        slot[1] += 1

                    if on_disk >= file_floor:
                        big_files.append((entry.path, on_disk, st.st_size,
                                          kind, who))
        except OSError:
            unreadable += 1

        # Charge this directory's own files to it and to every parent, so a
        # total is what it contains rather than only what sits directly in it.
        if here_bytes or here_files:
            # Up to whichever root this path is under. Stopping at roots[0]
            # would credit nothing outside home to anything.
            # Exactly one root can contain this: _prune dropped any that were
            # inside another, so there is nothing to choose between.
            mine = ""
            for candidate in roots:
                if here == candidate or here.startswith(candidate + os.sep):
                    mine = candidate
                    break
            walk = here
            while True:
                dir_bytes[walk] = dir_bytes.get(walk, 0) + here_bytes
                dir_files[walk] = dir_files.get(walk, 0) + here_files
                if not mine or walk == mine or len(walk) <= len(mine):
                    break
                walk = os.path.dirname(walk)

        if progress and time.time() - last_report > 1.0:
            last_report = time.time()
            progress(files, total)
        if files >= max_files:
            stopped = "stopped after %d files" % max_files
            break
        if deadline and time.time() > deadline:
            stopped = "stopped after %d seconds" % int(deadline - started)
            break

    big_files.sort(key=lambda f: -f[1])
    big_files = big_files[:400]

    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO space_dir (scan_id, path, depth, bytes, "
            "files, owner) VALUES (?,?,?,?,?,?)",
            [(scan_id, path, path.count(os.sep), size, dir_files.get(path, 0),
              owner_of(path))
             for path, size in dir_bytes.items() if size >= floor])
        conn.executemany(
            "INSERT OR REPLACE INTO space_file (scan_id, path, bytes, logical, "
            "kind, owner) VALUES (?,?,?,?,?,?)",
            [(scan_id, p, b, l, k, o) for p, b, l, k, o in big_files])
        conn.executemany(
            "INSERT OR REPLACE INTO space_kind (scan_id, kind, bytes, files) "
            "VALUES (?,?,?,?)",
            [(scan_id, k, v[0], v[1]) for k, v in kinds.items()])
        conn.executemany(
            "INSERT OR REPLACE INTO space_owner (scan_id, owner, bytes, files) "
            "VALUES (?,?,?,?)",
            [(scan_id, o, v[0], v[1]) for o, v in owners.items()])
        conn.execute(
            "UPDATE space_scan SET finished_ts=?, files=?, dirs=?, bytes=?, "
            "logical=?, unreadable=?, stopped=? WHERE id=?",
            (int(time.time()), files, dirs, total, logical_total, unreadable,
             stopped, scan_id))
        # One scan is kept. The point is what is on the disk now, and a history
        # of what used to be on it is a second thing taking up space.
        conn.execute("DELETE FROM space_scan WHERE id != ?", (scan_id,))
        for table in ("space_dir", "space_file", "space_kind", "space_owner"):
            conn.execute("DELETE FROM %s WHERE scan_id != ?" % table, (scan_id,))
    return scan_id


def reconcile(conn):
    """What the volume says, what was measured, and what the difference is.

    The number people notice first is whether these agree. They usually cannot
    agree exactly -- macOS keeps snapshots, other users have home folders, and
    some places refuse to be read at all -- so the difference is stated and
    named rather than left for somebody to find by subtracting.
    """
    found = latest(conn)
    vol = volumes()["data"]
    if not found or not found["finished_ts"] or not vol:
        return None
    missing = max(0, vol["used"] - found["bytes"])
    # The causes, named. Not one number with a paragraph of possible
    # explanations after it: that reads as "a third of your disk is a mystery",
    # and most of it never was one -- it was the other volumes in the
    # container, or a folder macOS refused to open.
    #
    # None of them carries a size, and that is the honest position rather than
    # a gap in the code: a folder that refused to be read weighs nothing here
    # precisely because it refused, and a snapshot's size is not a thing a
    # walk can measure. So the difference is stated once and left undivided.
    # The other volumes in the container are NOT part of this gap: `used` is
    # the data volume's own consumption now, and they sit outside it. They are
    # reported beside it, because they are why the free space on a 245 GB disk
    # is not 245 minus what is here.
    others = [v for v in (vol.get("others") or []) if v["used"]]
    parts = []
    blocked = guarded()
    if blocked:
        parts.append({
            "what": "%d folder%s macOS will not let Procwatch read"
                    % (len(blocked), "" if len(blocked) == 1 else "s"),
            "bytes": 0,
            "why": "including %s. Being refused looks exactly like an empty "
                   "folder, so these weigh nothing here. Full Disk Access in "
                   "System Settings, Privacy & Security changes that."
                   % blocked[0].split("/")[-1]})
    if found.get("unreadable"):
        parts.append({
            "what": "%d folder%s the scan could not open"
                    % (found["unreadable"],
                       "" if found["unreadable"] == 1 else "s"),
            "bytes": 0,
            "why": "permission denied while walking. Their contents are not "
                   "in any number on this page."})
    snaps = snapshots()
    if snaps:
        parts.append({
            "what": "%d Time Machine snapshot%s"
                    % (len(snaps), "" if len(snaps) == 1 else "s"),
            "bytes": 0,
            "why": "they hold the blocks of files already deleted, count as "
                   "used, and macOS thins them on its own. Their size is not "
                   "something a scan can measure."})
    if found.get("stopped"):
        parts.append({"what": "a scan that stopped early", "bytes": 0,
                      "why": found["stopped"] + ", so everything after that "
                                                "is missing from these totals."})
    # The parts of this volume the walk never visits. Named, because they are
    # nameable: the old wording said "other users' home folders" and dropping
    # it made the panel explain less than the version it replaced, while the
    # space itself stayed exactly where it was.
    parts.append({
        "what": "places on this volume the scan does not walk",
        "bytes": 0,
        "why": "other users' home folders, /Users/Shared, and the parts of "
               "the system outside %s. They are on this volume and are "
               "nobody's to clear from here."
               % ", ".join(SYSTEM_ROOTS)})
    # Compression and clones are last because they are not a folder anyone can
    # go and look at, and unlike everything above them they push the measured
    # total the other way.
    parts.append({
        "what": "files the disk stores smaller than they are",
        "bytes": 0,
        "why": "macOS compresses much of /Applications and /opt, and an APFS "
               "clone shares its blocks with the file it came from. Both are "
               "measured as what they occupy, which is less than what they "
               "contain."})
    reasons = ["%s -- %s" % (p["what"], p["why"]) for p in parts]
    return {"used": vol["used"], "scanned": found["bytes"], "missing": missing,
            "percent": round(missing / vol["used"] * 100, 1) if vol["used"] else 0,
            "parts": parts, "blocked": blocked, "reasons": reasons,
            "others": others,
            "container_used": vol.get("container_used") or 0}


def latest(conn):
    init(conn)
    row = conn.execute(
        "SELECT id, root, started_ts, finished_ts, files, dirs, bytes, logical, "
        "unreadable, stopped FROM space_scan ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    keys = ("id", "root", "started_ts", "finished_ts", "files", "dirs", "bytes",
            "logical", "unreadable", "stopped")
    # roots is read separately so the column order above stays as it was.
    return dict(zip(keys, row))


def _rows(conn, sql, args, keys):
    return [dict(zip(keys, r)) for r in conn.execute(sql, args)]


def _like_prefix(text):
    """A LIKE pattern matching everything under this exact string.

    `%` and `_` are wildcards and appear in real paths -- `_` in particular is
    in half the folder names on a developer's disk. Escaped with a backslash,
    which every query using this declares with ESCAPE.
    """
    for char in ("\\", "%", "_"):
        text = text.replace(char, "\\" + char)
    return text + "%"


def kinds(conn, scan_id):
    return _rows(conn,
                 "SELECT kind, bytes, files FROM space_kind WHERE scan_id=? "
                 "ORDER BY bytes DESC", (scan_id,), ("kind", "bytes", "files"))


def owners(conn, scan_id, limit=25):
    return _rows(conn,
                 "SELECT owner, bytes, files FROM space_owner WHERE scan_id=? "
                 "AND owner != '' ORDER BY bytes DESC LIMIT ?",
                 (scan_id, limit), ("owner", "bytes", "files"))


def biggest_files(conn, scan_id, under=None, limit=40):
    """The biggest files the scan found, minus the ones no longer there.

    A scan is a photograph, and every row here comes with a button that acts on
    the disk as it is now. Offering to move a file that was deleted last week
    is a row that can only fail, so the list is checked against the disk before
    it is shown -- a few dozen lstats against a page nobody opens per second.

    `under` scopes the list to one folder and everything below it, so drilling
    into ~/Movies shows the biggest files *there* rather than the same global
    fifteen on every level.
    """
    where = "WHERE scan_id=?"
    args = [scan_id]
    if under:
        under = os.path.abspath(os.path.expanduser(under))
        where += " AND path LIKE ? ESCAPE '\\'"
        args.append(_like_prefix(under.rstrip(os.sep) + os.sep))
    rows = _rows(conn,
                 "SELECT path, bytes, logical, kind, owner FROM space_file "
                 "%s ORDER BY bytes DESC LIMIT ?" % where,
                 args + [limit * 2],
                 ("path", "bytes", "logical", "kind", "owner"))
    return [row for row in rows if os.path.lexists(row["path"])][:limit]


def biggest_dirs(conn, scan_id, under=None, limit=30):
    """The biggest folders directly inside `under`, or the top level of the scan.

    One level at a time on purpose. A flat list of the biggest directories
    anywhere is a list of a folder and then all of its parents, which tells you
    nothing you did not already know from the first row.
    """
    row = conn.execute("SELECT root, roots FROM space_scan WHERE id=?",
                       (scan_id,)).fetchone()
    if not row:
        return []
    roots = [r for r in (row[1] or "").split("\n") if r] or [row[0]]
    if not under:
        # The top level is the roots themselves, so home sits beside
        # /Applications rather than the two being different kinds of thing.
        if len(roots) > 1:
            found = _rows(conn,
                          "SELECT path, bytes, files, owner FROM space_dir "
                          "WHERE scan_id=? AND path IN (%s) ORDER BY bytes DESC"
                          % ",".join("?" * len(roots)),
                          [scan_id] + roots,
                          ("path", "bytes", "files", "owner"))
            if found:
                return found
        under = roots[0]
    under = os.path.abspath(os.path.expanduser(under))
    depth = under.count(os.sep) + 1
    # ESCAPE, because `_` is a wildcard in LIKE and folders are full of them:
    # without it, opening ~/my_app also listed the contents of ~/myXapp.
    return _rows(conn,
                 "SELECT path, bytes, files, owner FROM space_dir "
                 "WHERE scan_id=? AND depth=? AND path LIKE ? ESCAPE '\\' "
                 "ORDER BY bytes DESC LIMIT ?",
                 (scan_id, depth,
                  _like_prefix(under.rstrip(os.sep) + os.sep), limit),
                 ("path", "bytes", "files", "owner"))


# ---------------------------------------------------------------------------
# What a big folder actually is.
#
# The same idea as the process catalogue, for paths. "~/Library/Containers is
# 40 GB" is a fact nobody can act on; "that is what sandboxed applications are
# allowed to write, and the big one inside it is Docker's virtual disk" is an
# answer. Everything here is a place people actually find at the top of a scan.
#
# `safe` marks what can be deleted without losing anything that cannot be made
# again -- and it is a hand-written list on purpose. "Anything called Caches"
# includes caches an application cannot rebuild.
# ---------------------------------------------------------------------------
NOTES = [
    ("~/Library/Caches", False,
     "Everything applications keep to avoid downloading or computing it twice. "
     "Safe to clear as a whole, but they refill; clear the big ones rather than "
     "all of them."),
    ("~/Library/Containers", False,
     "Sandboxed applications keep everything here, including their real data. "
     "Never delete a container outright: for a sandboxed app it is the app's "
     "documents, not its cache."),
    ("~/Library/Group Containers", False,
     "Data shared between an application and its extensions. Same warning as "
     "Containers."),
    ("~/Library/Application Support", False,
     "Where applications keep the data they cannot recreate. This is usually "
     "the largest thing in a home directory and the least safe to touch."),
    ("~/Library/Developer/Xcode/DerivedData", True,
     "Xcode's build output. Rebuilt on the next build, always safe to delete, "
     "and routinely tens of gigabytes."),
    ("~/Library/Developer/Xcode/Archives", False,
     "Every build you have archived for distribution, kept forever. Safe to "
     "delete the old ones, but they are the only copy of what you shipped."),
    ("~/Library/Developer/Xcode/iOS DeviceSupport", True,
     "Debug symbols for every iOS version you have ever attached a device from. "
     "Re-downloaded when needed. Frequently 20 GB or more."),
    ("~/Library/Developer/CoreSimulator/Devices", True,
     "Every simulator you have ever booted, kept whole. `xcrun simctl delete "
     "unavailable` removes the ones for SDKs you no longer have."),
    ("~/Library/Caches/com.apple.dt.Xcode", True,
     "Xcode's own cache. Rebuilt."),
    ("~/Library/Containers/com.docker.docker", False,
     "Docker's virtual disk. It grows and does not shrink on its own -- "
     "`docker system prune` reclaims inside it, and Docker Desktop's settings "
     "can shrink the file afterwards."),
    ("~/Library/Application Support/MobileSync/Backup", False,
     "iPhone and iPad backups. Often the single largest folder on a Mac. Each "
     "is a full device; delete the old ones from Finder rather than here."),
    ("~/Library/Mail", False,
     "Every message and attachment kept offline. Mail's own settings decide how "
     "much is downloaded."),
    ("~/Library/Messages", False,
     "iMessage history and every attachment ever received."),
    ("~/Library/Logs", True,
     "Application logs. Safe to clear."),
    # Not safe -- not because it is precious, but because of what the button
    # beside a safe entry does. Everything here is moved to the Trash, so
    # offering the Trash offered to move it into itself: Finder was asked to
    # delete ~/.Trash, and the hand-move fallback tried
    # shutil.move(~/.Trash, ~/.Trash/.Trash). Emptying it is a permanent
    # delete, which is the one thing this file will not do, so it is described
    # and left to Finder.
    ("~/.Trash", False,
     "Already deleted, still occupying the disk until emptied. Emptying is "
     "permanent, so it is Finder's to do rather than this."),
    ("~/Library/Caches/Homebrew", True,
     "Downloaded bottles Homebrew has already installed. `brew cleanup` does "
     "this properly."),
    ("~/Library/Caches/pip", True, "Python wheels pip has already installed."),
    ("~/Library/Caches/Yarn", True, "Yarn's package cache."),
    ("~/.npm/_cacache", True, "npm's package cache. Rebuilt on demand."),
    ("~/.cache", True, "Where cross-platform tools put their caches."),
    ("~/.cargo/registry", True, "Rust crates already compiled."),
    ("~/Library/Caches/CloudKit", False,
     "Local copies of iCloud data. macOS manages this and refills it."),
    ("~/Downloads", False,
     "Not a system folder, but usually the fastest few gigabytes anybody has "
     "ever recovered."),
    ("~/Movies", False, "Includes iMovie and Final Cut libraries, which are "
                        "large and are not backups of anything."),
]

_NOTE_INDEX = [(os.path.expanduser(p), safe, text) for p, safe, text in NOTES]


def explain(path):
    """What this folder is, and whether it can go.

    Longest match wins, so ~/Library/Caches/Homebrew is answered by the entry
    about Homebrew rather than the one about caches in general.
    """
    path = os.path.abspath(os.path.expanduser(path))
    best = None
    for prefix, safe, text in _NOTE_INDEX:
        if path == prefix or path.startswith(prefix + os.sep):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, safe, text)
    if not best:
        return None
    return {"about": best[0].replace(os.path.expanduser("~"), "~"),
            "safe": best[1], "why": best[2]}


def caches(conn=None):
    """The things that can be cleared, measured, with what each one costs.

    Only what is on the list above and marked safe, and only what exists. The
    size is measured now rather than read from the last scan: somebody deciding
    whether to delete something wants today's number.
    """
    out = []
    for prefix, safe, why in NOTES:
        if not safe:
            continue
        path = os.path.expanduser(prefix)
        if not os.path.isdir(path):
            continue
        # Per entry, not shared across them: two entries on this list do not
        # overlap, and a link counted against one should still be counted
        # against the other if it somehow appears there too.
        links = set()
        total = files = 0
        for root, dirs, names in os.walk(path):
            for name in names:
                try:
                    st = os.lstat(os.path.join(root, name))
                except OSError:
                    continue
                if counted_once(links, st):
                    total += st.st_blocks * 512
                files += 1
        if total == 0:
            continue
        out.append({"path": prefix, "full_path": path, "bytes": total,
                    "files": files, "why": why})
    out.sort(key=lambda c: -c["bytes"])
    return out


# ---------------------------------------------------------------------------
# Removing things.
#
# Into the Trash, never unlinked. A wrong call here costs somebody their work,
# and the difference between a mistake you drag back and one you restore from a
# backup you may not have is the whole design.
# ---------------------------------------------------------------------------
def _is_app_bundle(path):
    """Whether this is an application sitting in a folder applications live in.

    Deliberately narrow: the bundle itself, directly inside one of those
    folders, and a real folder rather than a file wearing the name. It does not
    admit /Applications, or /Applications/Utilities, or anything nested inside a
    bundle, and /System/Applications is not on the list at all.
    """
    if not path.endswith(".app") or not os.path.isdir(path):
        return False
    parent = os.path.dirname(path)
    return any(parent == os.path.abspath(os.path.expanduser(folder))
               for folder in APP_FOLDERS)


def _protected(path):
    """Whether this is somewhere nothing here may touch."""
    home = os.path.expanduser("~").rstrip(os.sep)
    path = os.path.abspath(path).rstrip(os.sep)
    # Home itself first. Testing "is it inside home" before "is it home" makes
    # the home directory come back as being outside itself, which is true of the
    # string and useless as an answer.
    if path == home:
        return "your entire home directory"
    # Applications are the one thing outside home that may be removed, and the
    # rule below would otherwise refuse every one of them -- which it did, so
    # the Uninstall button could not work at all: it refused /Applications/X.app
    # for being outside home, having been written for a feature that only ever
    # deleted files in it.
    if _is_app_bundle(path):
        return ""
    if not path.startswith(home + os.sep):
        return "outside your home directory"
    # The recording itself, and the program.
    from . import config
    for guard in (config.DB_PATH, os.path.dirname(config.DB_PATH),
                  os.path.expanduser("~/Library/Application Support/procwatch")):
        guard = os.path.abspath(guard)
        if path == guard or guard.startswith(path + os.sep):
            return "part of Procwatch itself"
    # Synced folders, and everything inside them. Refused by subtree rather
    # than by equality, because the danger here is not the folder: deleting one
    # file under CloudStorage or Mobile Documents deletes it from iCloud,
    # Dropbox or Google Drive, and from every other machine signed in to them.
    # A local Trash is not an undo for that.
    # Inside the Trash. Not precious, but a button that moves a file from the
    # Trash to the Trash renames it and reports success, which is a lie about
    # having done something. Biggest single files reaches in here.
    trash_dir = os.path.join(home, ".Trash")
    if path == trash_dir or path.startswith(trash_dir + os.sep):
        return "already in the Trash -- emptying it is Finder's to do"
    for synced, what in ((os.path.join(home, "Library/CloudStorage"),
                          "inside a synced cloud folder -- removing it here "
                          "removes it from the service and every other device"),
                         (os.path.join(home, "Library/Mobile Documents"),
                          "inside iCloud Drive -- removing it here removes it "
                          "from iCloud and every other device")):
        if path == synced or path.startswith(synced + os.sep):
            return what
    for risky in ("Library/Containers", "Library/Group Containers",
                  "Library/Application Support", "Library/Mail",
                  "Library/Messages", "Documents", "Desktop", "Pictures",
                  # The whole of ~/Library, now that folder rows carry a
                  # Trash button too. Everything a running session depends on
                  # lives under it, and "restore from the Trash" is not an
                  # undo for a login that can no longer start.
                  "Library",
                  # The folder, not an application in it. Allowing bundles
                  # through made the folder holding them reachable too.
                  "Applications"):
        full = os.path.join(home, risky)
        if path == full:
            return "an entire application data folder"
    return ""


# Paths are passed to this as arguments, never built into it. `on run argv`
# receives them as strings the interpreter has already finished parsing, so a
# file called `evil" & (do shell script "...") & "x` is a name and not code.
#
# The previous version interpolated each path into the source and escaped only
# the double quotes. That is wrong in a way worth writing down: a backslash was
# left alone, so a filename ending in one escaped its own closing quote and the
# string ran on into whatever followed. What that produced here was a syntax
# error -- one odd name and the whole batch silently failed to delete -- and
# whether a working payload can be built out of it is not a question worth
# answering when the fix removes the parse step entirely.
#
# The variable is not called `target`, and the name matters. Inside a Finder
# tell block `target` is Finder's own property, so `delete target` asked Finder
# for the target of a window it did not have and every removal failed with
# -1728 "Can't get target". Nothing said so: the fallback below moved whatever
# this process could move itself, which is most things, and quietly could not
# move the rest -- a root-owned application from the App Store stayed where it
# was while the panel reported it gone. A variable named after something Finder
# already owns is the whole bug, so the name here is one Finder has never heard
# of, and a test asserts it stays that way.
# The timeout is not decoration. Finder answers Apple events one at a time and
# puts up its own dialogs -- "are you sure", "you do not have permission" --
# which nobody sees when Finder is not the front application. Without this the
# default is two minutes of a blocked HTTP handler and then an error anyway.
_TRASH_SCRIPT = """on run argv
    set doomed to {}
    repeat with p in argv
        set end of doomed to POSIX file (p as text)
    end repeat
    with timeout of 90 seconds
        tell application "Finder" to delete doomed
    end timeout
end run"""


def _ask_finder(paths):
    """Ask Finder to move these to the Trash. Returns (ok, error)."""
    try:
        done = subprocess.run(["osascript", "-e", _TRASH_SCRIPT] + list(paths),
                              capture_output=True, text=True, timeout=180)
        return done.returncode == 0, (done.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as problem:
        return False, str(problem)


def _by_hand(paths):
    """Move them into ~/.Trash ourselves, when Finder will not.

    A fallback rather than the first choice: Finder records where each item came
    from, so the Trash offers Put Back, and moving them by hand loses that. But
    Finder can be busy or scripting can be refused, and "your disk is full and
    the button does nothing" is a worse outcome than a Trash without put-back.

    Names are made unique rather than overwritten. Two caches called Cache
    arriving in the same Trash must not become one.

    One path failing does not end the batch. It used to: an application whose
    bundle macOS would not let this move took its caches and its preferences
    down with it, and every remaining path was reported with the first one's
    error. What is returned is whether everything moved, and the first thing
    that went wrong -- the caller decides per path by looking at the disk.
    """
    trash_dir = os.path.expanduser("~/.Trash")
    try:
        os.makedirs(trash_dir, exist_ok=True)
    except OSError as problem:
        return False, str(problem)
    import shutil
    first = ""
    for path in paths:
        base = os.path.basename(path.rstrip(os.sep)) or "item"
        target = os.path.join(trash_dir, base)
        n = 1
        # lexists: a dangling symlink already in the Trash occupies the name it
        # sits on, and exists() looks through it and says the name is free.
        while os.path.lexists(target):
            stem, ext = os.path.splitext(base)
            target = os.path.join(trash_dir, "%s %d%s" % (stem, n, ext))
            n += 1
        try:
            shutil.move(path, target)
        except OSError as problem:
            first = first or str(problem)
    return (not first), first


def trash(paths, runner=None):
    """Move these to the Trash. Returns what happened to each.

    Finder is asked to do it, so the files land in the Trash with their
    put-back information and can be restored with one gesture. Moving them by
    hand into ~/.Trash loses that, and unlinking them loses everything.

    `runner` is given the list of paths, not a script, and exists so this can be
    tested without a real Finder. That is not politeness about the test suite:
    mutation-testing the refusals means deliberately breaking them and running
    the tests, and with a live runner that asks Finder to delete whatever the
    test named. It was refused -- the path was under /System and protected by
    the system -- but a guard that is only load-bearing when it works is not a
    guard.
    """
    results = []
    wanted = []
    for path in paths:
        path = os.path.abspath(os.path.expanduser(path))
        # lexists, not exists, everywhere in here. os.path.exists follows the
        # link and answers about what is on the far side, so a broken symlink
        # -- which is a real file taking a real directory entry -- reads as not
        # there before the move and as moved after it. That is the same false
        # success this function was rewritten to stop reporting.
        if not os.path.lexists(path):
            results.append({"path": path, "ok": False, "error": "no longer there"})
            continue
        refuse = _protected(path)
        if refuse:
            results.append({"path": path, "ok": False,
                            "error": "refused: that is %s" % refuse})
            continue
        wanted.append(path)

    if wanted:
        # One call for the batch: a Finder round trip each would take visible
        # seconds over a few hundred files.
        ok, error = (runner or _ask_finder)(wanted)
        # Finder's answer is one answer for the whole batch, and it is not
        # evidence. It returns success for a batch it moved only part of, so
        # what is reported per path is whether the path is still on the disk --
        # the question the caller is actually asking. Reporting Finder's word
        # instead is how "Moved to the Trash" appeared over an application that
        # had not moved.
        stuck = [path for path in wanted if os.path.lexists(path)]
        if stuck and runner is None:
            # Finder busy, refused, or unable. The files still go to the Trash,
            # just without Finder's put-back record.
            _, problem = _by_hand(stuck)
            error = error or problem
            stuck = [path for path in stuck if os.path.lexists(path)]
        for path in wanted:
            gone = path not in stuck
            results.append({"path": path, "ok": gone,
                            "error": "" if gone else (error or "Finder refused")})
    return results


# ---------------------------------------------------------------------------
# Removing an application, and everything it left behind.
#
# Dragging an app to the Bin removes the bundle and nothing else. What stays is
# usually larger than what went: the caches, the container, the saved state, the
# preferences, the cookies, the logs. This finds them and offers the lot.
#
# Matched on the bundle identifier wherever possible, and on the exact
# application name otherwise. Never on a substring: an uninstaller that matches
# loosely is one that deletes somebody else's data, and "Notes" appears inside
# a great many folder names that have nothing to do with Notes.
# ---------------------------------------------------------------------------

# Where an application is allowed to put things, and what each one is. `by`
# says which key fills the %s: the bundle identifier, or the application's name.
LEAVINGS = (
    ("Library/Application Support/%s", "ident", "its data"),
    ("Library/Application Support/%s", "name", "its data"),
    ("Library/Containers/%s", "ident", "its sandbox, including its documents"),
    ("Library/Group Containers/%s", "ident", "data shared with its extensions"),
    ("Library/Caches/%s", "ident", "its cache"),
    ("Library/Caches/%s", "name", "its cache"),
    ("Library/HTTPStorages/%s", "ident", "cookies and website data"),
    ("Library/HTTPStorages/%s.binarycookies", "ident", "cookies"),
    ("Library/WebKit/%s", "ident", "web content it stored"),
    ("Library/Preferences/%s.plist", "ident", "its settings"),
    ("Library/Preferences/%s.helper.plist", "ident", "its helper's settings"),
    ("Library/Saved Application State/%s.savedState", "ident",
     "the windows it had open"),
    ("Library/Cookies/%s.binarycookies", "ident", "cookies"),
    ("Library/Logs/%s", "name", "its logs"),
    ("Library/Logs/%s", "ident", "its logs"),
    ("Library/LaunchAgents/%s.plist", "ident", "a job that starts it at login"),
    ("Library/Application Scripts/%s", "ident", "scripts it was allowed to run"),
    ("Library/Autosave Information/%s", "ident", "unsaved documents"),
)

# Group containers are named group.<something>, and the something is usually the
# identifier with the leading component replaced by a team id. Matching those
# needs a prefix rather than an exact name, so they are handled separately and
# only on the identifier's own suffix.
def _group_containers(ident):
    """
    The suffix rule is deliberately narrow. `endswith("." + tail)` on its own
    contradicts the rule above it: for a two-component identifier like
    net.whatsapp the tail is the single word `whatsapp`, and any
    group.anything.whatsapp matched -- somebody else's data, removed on the
    strength of one word. So the tail is only used when it has a dot in it,
    and only against a single leading component, which is the team id shape
    the rule was written for.
    """
    home = os.path.expanduser("~")
    root = os.path.join(home, "Library/Group Containers")
    tail = ".".join(ident.split(".")[1:])          # com.foo.Bar -> foo.Bar
    if not tail:
        return []
    found = []
    try:
        names = os.listdir(root)
    except OSError:
        return []
    for name in names:
        # group.com.foo.Bar, or TEAMID.com.foo.Bar
        matched = (name == ident or name == "group." + ident
                   or name.endswith("." + ident))
        if not matched and "." in tail and name.endswith("." + tail):
            lead = name[:-(len(tail) + 1)]
            matched = bool(lead) and "." not in lead
        if matched:
            found.append(os.path.join(root, name))
    return found


def counted_once(seen, st):
    """Whether this file's blocks should be added, given what has been added.

    A hard link is one file with several names. Counting it once per name is
    how a folder of links reports a size the disk does not have -- Xcode's
    caches and Homebrew's Cellar are full of them. The identity is the pair
    (device, inode); only files with more than one link are remembered, so the
    set stays small on the millions of files that have exactly one.

    APFS clones are a different thing and are not solved here: a clone is two
    inodes sharing extents, and stat reports the full size for both. Nothing
    short of asking the filesystem for its extent map can see that, so a clone
    is counted twice by every tool that walks a disk, this one included.
    """
    if st.st_nlink <= 1:
        return True
    key = (st.st_dev, st.st_ino)
    if key in seen:
        return False
    seen.add(key)
    return True


def _folder_size(path, links=None):
    links = set() if links is None else links
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    if not os.path.isdir(path) or os.path.islink(path):
        return st.st_blocks * 512 if counted_once(links, st) else 0
    total = 0
    for root, dirs, names in os.walk(path):
        for name in names:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            if counted_once(links, st):
                total += st.st_blocks * 512
    return total


def app_info(app_path):
    """The name and bundle identifier of an application bundle."""
    app_path = os.path.abspath(os.path.expanduser(app_path))
    if not app_path.endswith(".app") or not os.path.isdir(app_path):
        return None
    name = os.path.basename(app_path)[:-4]
    ident = ""
    try:
        with open(os.path.join(app_path, "Contents", "Info.plist"), "rb") as fh:
            ident = plistlib.load(fh).get("CFBundleIdentifier") or ""
    except Exception:
        pass
    return {"path": app_path, "name": name, "ident": ident}


def leftovers(app_path):
    """Everything this application put outside its own bundle.

    Returns the bundle and each leftover with its size and what it is, so the
    list can be read before anything happens to it. Nothing is removed here.
    """
    info = app_info(app_path)
    if not info:
        return None
    home = os.path.expanduser("~")
    seen = set()
    # One set of hard links across the whole list, not one per item. A cache
    # and a container holding links to the same files are two entries here, and
    # measuring them separately promised back twice the space removing them
    # would return -- which is the number this page exists to get right.
    links = set()
    items = []

    def add(path, why, kind):
        path = os.path.abspath(path)
        if path in seen or not os.path.lexists(path):
            return
        seen.add(path)
        items.append({"path": path, "display": path.replace(home, "~"),
                      "bytes": _folder_size(path, links), "why": why,
                      "kind": kind})

    add(info["path"], "the application itself", "app")
    for pattern, by, why in LEAVINGS:
        value = info["ident"] if by == "ident" else info["name"]
        if not value:
            continue
        add(os.path.join(home, pattern % value), why, "leftover")
    if info["ident"]:
        for path in _group_containers(info["ident"]):
            add(path, "data shared with its extensions", "leftover")
        # Preferences kept per host, which are named with a hardware UUID and
        # so cannot be guessed -- listed rather than constructed.
        byhost = os.path.join(home, "Library/Preferences/ByHost")
        try:
            for name in os.listdir(byhost):
                if name.startswith(info["ident"] + "."):
                    add(os.path.join(byhost, name), "its settings", "leftover")
        except OSError:
            pass

    total = sum(i["bytes"] for i in items)
    return {"app": info, "items": items, "bytes": total,
            "leftover_bytes": sum(i["bytes"] for i in items
                                  if i["kind"] == "leftover")}


APP_FOLDERS = ("/Applications", "/Applications/Utilities", "~/Applications")


def _bundle_bytes(path):
    """What one application bundle occupies, measured directly.

    Only needed for bundles the scan left out. The scan records folders above a
    floor, because a listing of every folder on a disk is not a diagnosis, but
    an application below that floor is still an application somebody may want
    gone -- twenty of the fifty here are, and they were simply missing.
    """
    total = 0
    links = set()
    # followlinks=False is the whole protection: os.walk will not descend into
    # a symlinked folder, so an application that links into a shared framework
    # or into the user's own folders is not charged for what is on the far side.
    for parent, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                info = os.lstat(os.path.join(parent, name))
            except OSError:
                continue
            if counted_once(links, info):
                total += info.st_blocks * 512
    return total


def installed():
    """Every application on disk, by path.

    Read from the folders rather than from the scan: the scan is a size
    ranking and an application that has since been removed still has a row in
    it, while one installed afterwards has none. /System/Applications is left
    out on purpose -- those cannot be removed, so offering to is a lie.
    """
    out = []
    seen = set()
    for folder in APP_FOLDERS:
        full = os.path.expanduser(folder)
        try:
            names = sorted(os.listdir(full))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".app"):
                continue
            path = os.path.join(full, name)
            if path in seen or not os.path.isdir(path):
                continue
            seen.add(path)
            out.append(path)
    return out


def applications(conn, scan_id, limit=None):
    """Installed applications, with what each one occupies in total.

    The bundle comes from the scan's folder totals where it has them and from
    a direct measurement where it does not, and the leftovers from the
    per-application totals the scan already computed. It is an estimate of the
    leftovers rather than the exact list -- pressing Uninstall goes and looks
    properly -- but it is the number that makes the case: an application whose
    bundle is 877 MB and whose leftovers are 3.2 GB is not a 877 MB
    application.
    """
    scanned = {row["path"]: row["bytes"]
               for row in _rows(conn,
                                "SELECT path, bytes FROM space_dir "
                                "WHERE scan_id=? AND path LIKE ? ESCAPE '\\'",
                                (scan_id, "%.app"), ("path", "bytes"))}
    owners = {row["owner"]: row["bytes"]
              for row in _rows(conn,
                               "SELECT owner, bytes FROM space_owner "
                               "WHERE scan_id=?", (scan_id,),
                               ("owner", "bytes"))}
    paths = installed()
    # Two applications of the same name -- one in /Applications, one in
    # ~/Applications -- share a single row in space_owner, because the owner is
    # a name and not a path. Charging that row to both would report the same
    # leftovers twice, so neither is given an estimate; pressing Uninstall
    # still goes and looks properly, which is where the real answer was always
    # going to come from.
    counts = {}
    for path in paths:
        name = os.path.basename(path)[:-4]
        counts[name] = counts.get(name, 0) + 1
    out = []
    for path in paths:
        name = os.path.basename(path)[:-4]
        bundle = scanned.get(path)
        if bundle is None:
            bundle = _bundle_bytes(path)
        # The per-application total already contains the bundle: files under
        # /Applications/X.app are attributed to X, the same as files under its
        # caches. Subtracting it is what makes "leftovers" mean what it says --
        # without it Xcode reported 4,220 MB of bundle and 4,224 MB left
        # behind, which is the bundle counted twice and four megabytes of truth.
        shared = counts.get(name, 0) > 1
        left = 0 if shared else max(0, owners.get(name, 0) - bundle)
        out.append({"path": path, "name": name, "ambiguous": shared,
                    "bundle": bundle, "leftover": left,
                    "bytes": bundle + left})
    out.sort(key=lambda a: -a["bytes"])
    return out[:limit] if limit else out


def running(app_path):
    """Whether this application is running, so it can be quit before it goes."""
    info = app_info(app_path)
    if not info:
        return False
    text = _run(["pgrep", "-fl", info["path"] + "/Contents/MacOS/"], timeout=10)
    return bool(text.strip())


# ---------------------------------------------------------------------------
# One-press cleanup.
#
# The same ground the Trash buttons above already cover, gathered into a
# single plan: the safe caches, plus what applications that are no longer
# installed left behind. Everything in the plan goes through trash() and its
# refusals; nothing here deletes.
# ---------------------------------------------------------------------------

# Where an uninstalled application's leavings are worth sweeping. Only the
# recreatable kinds: a cache, a log, or a saved window position costs nothing
# even when the match is wrong, which is the property that lets a sweep run
# without a person checking every row. Application Support and Containers are
# deliberately absent -- for a false match there, the mistake is somebody's
# data.
_ORPHAN_DIRS = ("~/Library/Caches", "~/Library/Logs",
                "~/Library/Saved Application State")


def _reverse_dns(name):
    """The bundle-identifier shape, "com.vendor.App", out of a folder name.

    Saved Application State appends ".savedState"; strip it first. Anything
    without at least three components is not treated as an identifier at all:
    a bare "Google" or "pip" folder names a vendor or a tool, not an
    application this can match against the installed list.
    """
    if name.endswith(".savedState"):
        name = name[:-len(".savedState")]
    parts = name.split(".")
    if len(parts) < 3 or not all(parts):
        return ""
    return name


def orphans(installed_idents=None):
    """What applications that are no longer installed left behind.

    Matched by bundle identifier, and only in the recreatable folders above.
    Apple's own identifiers are skipped wholesale: macOS itself is not an
    application whose absence can be established by looking in /Applications.
    So are identifiers whose owning bundle still exists anywhere on the
    installed list -- and, since a helper's identifier usually shares the
    vendor's leading components with its parent ("com.microsoft.teams2" under
    "com.microsoft"), anything whose first two components match an installed
    identifier's is kept too.
    """
    if installed_idents is None:
        installed_idents = set()
        for path in installed():
            info = app_info(path)
            if info and info["ident"]:
                installed_idents.add(info["ident"])
    vendors = {".".join(ident.split(".")[:2]) for ident in installed_idents}
    home = os.path.expanduser("~")
    links = set()
    found = []
    for prefix in _ORPHAN_DIRS:
        root = os.path.expanduser(prefix)
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            ident = _reverse_dns(name)
            if not ident or ident.startswith("com.apple."):
                continue
            if ident in installed_idents:
                continue
            if ".".join(ident.split(".")[:2]) in vendors:
                continue
            path = os.path.join(root, name)
            if not os.path.isdir(path):
                continue
            size = _folder_size(path, links)
            if size <= 0:
                continue
            found.append({"path": path, "display": path.replace(home, "~"),
                          "bytes": size, "ident": ident})
    found.sort(key=lambda item: -item["bytes"])
    return found


def cleanup_plan():
    """Everything one press of Clean up would move to the Trash.

    Two groups: the caches already marked safe, and the leavings of
    applications that are gone. Overlaps are removed -- a safe cache that is
    also an orphan is one folder on the disk, and a plan that lists it twice
    promises back twice the space removing it returns.
    """
    groups = []
    seen = set()
    cached = []
    for entry in caches():
        path = os.path.abspath(entry["full_path"])
        seen.add(path)
        cached.append({"path": path, "display": entry["path"],
                       "bytes": entry["bytes"], "why": entry["why"]})
    if cached:
        groups.append({"key": "caches", "title": "Caches that refill",
                       "items": cached})
    left = [item for item in orphans()
            if os.path.abspath(item["path"]) not in seen
            and not any(os.path.abspath(item["path"]).startswith(p + os.sep)
                        for p in seen)]
    if left:
        groups.append({"key": "orphans",
                       "title": "Left behind by removed applications",
                       "items": left})
    return {"groups": groups,
            "bytes": sum(i["bytes"] for g in groups for i in g["items"])}
