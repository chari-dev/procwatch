"""An application's own icon, as a PNG the dashboard can show.

Two letters in a coloured square is what a program shows when it does not
know what something looks like. macOS does know: every application carries
its icon inside its bundle, so this digs it out and converts it once.

The path is deliberately narrow -- read CFBundleIconFile, find the .icns
beside it, hand it to sips. Quick Look would render anything, including the
applications that keep their icon in an asset catalogue rather than a file,
but qlmanage hung for a minute and a half on the first bundle it was asked
about here, and an interface that stalls waiting for decoration is worse
than one that shows initials. What has no .icns keeps its initials.

Converted icons are cached on disk, keyed by the bundle and how recently it
changed, so an application is converted once per update rather than once
per page.
"""
import hashlib
import os
import plistlib
import subprocess
import time

from . import config

# Rendered at twice the size anything is drawn at, so it stays sharp on a
# retina display without shipping a 1024px icon to the page.
SIZES = (32, 64, 128)
DEFAULT_SIZE = 64

# Where an application may live. An icon request names a path, so the answer
# to "which paths" has to be a list rather than "whatever you ask for".
APP_ROOTS = ("/Applications", "/System/Applications", "/Developer",
             "/System/Library/CoreServices", "/Library/Application Support",
             "/System/Library/PrivateFrameworks", "/usr/local",
             "/opt", os.path.expanduser("~/Applications"))

_MARKER = ".app/Contents/MacOS/"


def cache_dir():
    path = os.path.join(os.path.dirname(config.LOG_PATH), "icons")
    os.makedirs(path, exist_ok=True)
    return path


def bundle_of(exe_path):
    """The application an executable belongs to, or "" for a plain program.

    /Applications/Arc.app/Contents/MacOS/Arc -> /Applications/Arc.app

    The outermost bundle, not the nearest one. Applications carry helpers
    inside themselves -- a renderer, a crash reporter, an updater -- each in
    its own .app under Contents/Frameworks, and those have no icon of their
    own. Reading the nearest bundle asked for an icon that does not exist
    and left GitHub Desktop showing initials while its icon sat two
    directories up.
    """
    if not exe_path:
        return ""
    if exe_path.endswith(".app"):
        return exe_path
    cut = exe_path.find(".app" + os.sep)
    if cut != -1:
        return exe_path[:cut + 4]
    return ""


_BY_NAME = {"at": 0.0, "map": {}}


def bundle_for_name(name):
    """The application called this, if one is installed.

    Most of the dashboard knows an application only by its name -- the
    charts, the timeline, the search results -- so this is what lets a name
    alone carry a picture. The listing is cached for a minute: it is a
    handful of directory reads, and it changes when something is installed,
    not between two rows of the same table.
    """
    if not name:
        return ""
    now = time.time()
    if now - _BY_NAME["at"] > 60:
        found = {}
        for root in ("/Applications", "/Applications/Utilities",
                     "/System/Applications", "/System/Applications/Utilities",
                     os.path.expanduser("~/Applications"),
                     # Finder, ControlCenter and the rest of the things that
                     # are applications but do not live where applications do.
                     "/System/Library/CoreServices"):
            try:
                entries = os.listdir(root)
            except OSError:
                continue
            for entry in entries:
                if entry.endswith(".app"):
                    # First one wins: /Applications before /System, so a
                    # replacement sits in front of Apple's original.
                    found.setdefault(entry[:-4].lower(),
                                     os.path.join(root, entry))
        _BY_NAME["map"] = found
        _BY_NAME["at"] = now
    return _BY_NAME["map"].get(name.strip().lower(), "")


def allowed(bundle):
    """Whether this is a real application bundle we will answer for.

    The request carries a path, so without this the endpoint would convert
    and return any .icns on the disk. Nothing here is secret -- they are
    icons -- but an endpoint that reads arbitrary paths is a habit worth not
    forming.
    """
    if not bundle or not bundle.endswith(".app"):
        return False
    # normpath, not realpath. It collapses "..", which is what stops a path
    # climbing out of the folders below -- while realpath also follows the
    # firmlinks macOS uses for its own applications, and /Applications/
    # Safari.app resolves onto a Cryptex volume that matches no root here.
    # Insisting on the resolved path refused Safari its own icon.
    bundle = os.path.normpath(os.path.abspath(bundle))
    if not os.path.isdir(bundle):
        return False
    return any(bundle.startswith(os.path.normpath(root) + os.sep)
               for root in APP_ROOTS if os.path.isdir(root))


def _icns_path(bundle):
    """The icon file inside a bundle, by name or by looking."""
    plist = os.path.join(bundle, "Contents", "Info.plist")
    named = ""
    try:
        with open(plist, "rb") as handle:
            info = plistlib.load(handle)
        named = info.get("CFBundleIconFile") or ""
    except Exception:
        named = ""
    resources = os.path.join(bundle, "Contents", "Resources")
    if named:
        if not named.endswith(".icns"):
            named += ".icns"
        found = os.path.join(resources, named)
        if os.path.isfile(found):
            return found
    # No name, or the name lied. One .icns in Resources is unambiguous;
    # several are a toolbar's worth of icons and none of them is the app's.
    try:
        every = [n for n in os.listdir(resources) if n.endswith(".icns")]
    except OSError:
        return ""
    if len(every) == 1:
        return os.path.join(resources, every[0])
    for name in every:
        if name.lower() in ("appicon.icns", "applicationicon.icns",
                            "app.icns", "icon.icns"):
            return os.path.join(resources, name)
    return ""


def _cache_key(bundle, size):
    try:
        stamp = int(os.path.getmtime(os.path.join(bundle, "Contents",
                                                  "Info.plist")))
    except OSError:
        stamp = 0
    digest = hashlib.sha1(("%s|%s" % (bundle, stamp)).encode()).hexdigest()[:16]
    return os.path.join(cache_dir(), "%s-%d.png" % (digest, size))


def png(bundle, size=DEFAULT_SIZE):
    """The application's icon as PNG bytes, or None if it has none.

    Cached on disk. A bundle whose icon cannot be converted is remembered as
    having none, so a page full of them does not run sips once per refresh
    for an answer that will not change.
    """
    if size not in SIZES:
        size = DEFAULT_SIZE
    if not allowed(bundle):
        return None
    cached = _cache_key(bundle, size)
    if os.path.exists(cached):
        if os.path.getsize(cached) == 0:
            return None          # known to have no icon
        try:
            with open(cached, "rb") as handle:
                return handle.read()
        except OSError:
            pass
    source = _icns_path(bundle)
    if source:
        try:
            done = subprocess.run(
                ["sips", "-s", "format", "png", "-Z", str(size),
                 source, "--out", cached],
                capture_output=True, timeout=20)
            if done.returncode == 0 and os.path.exists(cached) \
                    and os.path.getsize(cached) > 0:
                with open(cached, "rb") as handle:
                    return handle.read()
        except (OSError, subprocess.TimeoutExpired):
            pass
    # Remember the absence, as an empty file.
    try:
        open(cached, "wb").close()
    except OSError:
        pass
    return None
