"""Turn a process's argv into a stable grouping key.

Two failure modes to avoid. Too coarse and `log stream --predicate X` merges
with every other `log`, hiding the culprit. Too fine and each of 27 browser
renderers becomes its own row because their argv differ by a PID.
"""
import os
import re

MAX_ARGS = 100

# Order matters: UUIDs are matched before bare digit runs, otherwise the digit
# rule chews through a UUID's segments and leaves an unstable remainder.
_VOLATILE = [
    (re.compile(r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"), "UUID"),
    (re.compile(r"\b\d+\b"), "N"),
]


def _split(comm, command):
    """Separate argv[0] from the rest.

    `command` is argv joined by spaces, and macOS executable paths contain
    spaces ("Activity Monitor"), so splitting on whitespace corrupts the
    boundary. `comm` is exactly argv[0], so the prefix gives it away.
    """
    if command == comm:
        return comm, []
    if command.startswith(comm + " "):
        return comm, command[len(comm) + 1:].split()
    # ps reports some processes in a form that does not extend comm at all
    # (kernel threads, "(thing)" for zombies). Keep the exe, drop the args.
    return comm, []


def _normalise(arg):
    if arg.startswith("/"):
        arg = os.path.basename(arg) or "/"
    for pattern, replacement in _VOLATILE:
        arg = pattern.sub(replacement, arg)
    return arg


def derive(comm, command):
    """Return (exe, args_sig) -- the interned identity of a process."""
    exe_path, args = _split(comm, command)
    exe = os.path.basename(exe_path) or exe_path
    sig = " ".join(_normalise(a) for a in args)
    return exe, sig[:MAX_ARGS]


# Apple ships its daemons out of these roots and third-party software does not
# get to write there, so the path is a reliable classifier without needing to
# read bundle identifiers or code signatures.
_SYSTEM_ROOTS = ("/System/", "/usr/libexec/", "/usr/sbin/", "/usr/bin/",
                 "/sbin/", "/bin/", "/Library/Apple/")

# Things that live under a system root but are the user's own doing, or are
# what someone actually means when they say "my apps".
_USER_ROOTS = ("/Applications/", "/opt/homebrew/", "/opt/local/", "/usr/local/")


def is_system(exe_path):
    """Whether a path belongs to macOS rather than to something you installed.

    Returns None when the argument carries no path at all, which is a real
    case: about 40 processes on a typical machine report a bare name, either
    because they rewrote their own argv (postgres names its workers
    "postgres: walwriter") or because no path is exposed (autofsd). Those
    cannot be judged here and must be resolved by the caller from the parent
    chain -- guessing would put postgres's workers in the system bucket and
    hide them.
    """
    if not exe_path or not exe_path.startswith("/"):
        return None
    for root in _USER_ROOTS:
        if exe_path.startswith(root):
            return False
    if exe_path.startswith("/Users/"):
        return False
    return exe_path.startswith(_SYSTEM_ROOTS)


def classify(processes):
    """{pid: is_system} for a whole process listing.

    A process with no path inherits from the nearest ancestor that has one,
    which is what makes "postgres: walwriter" come out as yours (its parent
    is the postgres binary under /opt/homebrew) while autofsd comes out as
    system (its only ancestor is launchd). Anything whose chain reaches
    launchd without ever finding a path is treated as system, since that is
    what an unattributable daemon almost always is.
    """
    paths, parents = {}, {}
    for proc in processes:
        paths[proc.pid] = proc.comm if proc.comm.startswith("/") else (
            proc.command if proc.command.startswith("/") else None)
        parents[proc.pid] = getattr(proc, "ppid", None)

    out = {}
    for pid in paths:
        seen, cursor, verdict = set(), pid, None
        while cursor is not None and cursor not in seen:
            seen.add(cursor)
            verdict = is_system(paths.get(cursor))
            if verdict is not None:
                break
            cursor = parents.get(cursor)
        out[pid] = True if verdict is None else verdict
    return out


# Bundles under here are the system's own interface -- Dock, WindowManager,
# Control Centre -- rather than applications anyone launched. Real apps, both
# Apple's and yours, live in /Applications or /System/Applications.
_UI_BUNDLE_ROOTS = ("/System/Library/",)


def app_of(exe_path):
    """The application a process belongs to, or None if it is not in one.

    macOS puts every application in a `.app` bundle, and a bundle nests: Arc's
    renderer lives at
    /Applications/Arc.app/Contents/Frameworks/.../Browser Helper.app/Contents/MacOS/...
    so the OUTERMOST .app is the one a person would name. Taking the innermost
    gives "Browser Helper", which is Arc's plumbing rather than an app anyone
    launched.

    A process outside any bundle -- a daemon, a CLI tool, a compiler -- has no
    application and returns None. That is the line between "the apps I am
    running" and everything else the machine is doing.
    """
    if not exe_path or not exe_path.startswith("/") or ".app/" not in exe_path:
        return None
    if exe_path.startswith(_UI_BUNDLE_ROOTS):
        return None
    head = exe_path.split(".app/", 1)[0]
    return head.rsplit("/", 1)[-1] or None


def apps(processes):
    """{pid: app name or None}, resolving helpers through their parent.

    A helper whose own path is not inside a bundle still belongs to whatever
    launched it, so the parent chain is walked the same way `classify` walks
    it -- without that, an app's XPC services scatter into "not an app".
    """
    # Only argv[0] is considered, never the whole command line: a shell whose
    # arguments happen to mention a bundle path would otherwise be attributed
    # to that application. `comm` is argv[0]; the joined command is not.
    paths, parents = {}, {}
    for proc in processes:
        paths[proc.pid] = proc.comm if proc.comm.startswith("/") else ""
        parents[proc.pid] = getattr(proc, "ppid", None)

    out = {}
    for pid in paths:
        seen, cursor, name = set(), pid, None
        while cursor is not None and cursor not in seen:
            seen.add(cursor)
            name = app_of(paths.get(cursor, ""))
            if name:
                break
            cursor = parents.get(cursor)
        out[pid] = name
    return out
