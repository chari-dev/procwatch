#!/usr/bin/env python3
"""Fold procwatch into one runnable file.

Everything here is stdlib and the dashboard is a single self-contained page,
so the whole tool collapses into one script with the modules inlined and the
HTML embedded. That is the shareable form: copy `procwatch.py` anywhere with
a Mac and a Python, run it, done -- no clone, no install, no packaging.

The bundle is generated rather than maintained. The repository stays the
source of truth; this exists so that sharing it does not require explaining
a directory layout.
"""
import base64
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Dependency order: a module may only import ones already inlined above it.
MODULES = [
    "config", "selfupdate", "identity", "psreader", "rusage", "netstat",
    "geoip", "netpeer", "battery", "icons", "system",
    "db", "archive", "alerts", "prefs", "storage", "power", "versions",
    "knowledge", "events", "space", "diagnose",
    "appbuild", "share", "peers", "sampler",
    "rollup", "procs", "live", "query", "server",
    "launchd", "main", "cli",
]

HEADER = '''#!/usr/bin/env python3
"""procwatch -- per-process history for macOS, in one file.

Generated from https://github.com/chari-dev/procwatch by tools/bundle.py.
Do not edit: change the source modules and regenerate.

    python3 procwatch.py install     # start recording every 30 seconds
    python3 procwatch.py open        # dashboard in a browser
    python3 procwatch.py status      # what is stored
    python3 procwatch.py uninstall   # stop, keeping the database

Requires macOS and Python 3.9+. Nothing else.
"""
import base64 as _b64
import sys as _sys
import types as _types

_PKG = _types.ModuleType("procwatch")
_PKG.__path__ = []
_sys.modules["procwatch"] = _PKG


def _install(name, source):
    """Register one inlined module under the procwatch package."""
    module = _types.ModuleType("procwatch." + name)
    module.__package__ = "procwatch"
    # server.py derives its static directory from __file__, which an exec'd
    # module does not have. The bundle serves the page from an embedded
    # string anyway, so the value only needs to exist, not to resolve.
    module.__file__ = "<procwatch bundle>/" + name + ".py"
    _sys.modules["procwatch." + name] = module
    setattr(_PKG, name, module)
    exec(compile(source, "procwatch/" + name + ".py", "exec"), module.__dict__)
    return module

'''

FOOTER = '''

_STATIC = _b64.b64decode(_INDEX_HTML_B64).decode("utf-8")
_NETMON = _b64.b64decode(_NETMON_HTML_B64).decode("utf-8")
_STORAGE = _b64.b64decode(_STORAGE_HTML_B64).decode("utf-8")
_BATTERY = _b64.b64decode(_BATTERY_HTML_B64).decode("utf-8")
_WORLD = _b64.b64decode(_WORLD_JS_B64).decode("utf-8")
_server = _sys.modules["procwatch.server"]


def _dashboard_embedded():
    """The page baked into this file, for every server that asks for it."""
    return _STATIC


def _netmonitor_embedded():
    """The network monitor page, from the same single file."""
    return _NETMON


def _storage_embedded():
    """The storage page, from the same single file."""
    return _STORAGE


def _battery_embedded():
    """The battery page, from the same single file."""
    return _BATTERY


def _world_embedded():
    """The country outlines, from the same single file."""
    return _WORLD


_server.dashboard_html = _dashboard_embedded
_server.netmonitor_html = _netmonitor_embedded
_server.storage_html = _storage_embedded
_server.battery_html = _battery_embedded
_server.world_js = _world_embedded


def _serve_embedded(self):
    """Serve the page from the string baked into this file.

    _token_of is reached through the server module rather than this file's
    globals: the function is defined in that module's namespace, and looking
    it up here finds nothing.
    """
    page = _STATIC.replace("__PROCWATCH_TOKEN__", _server._token_of(self.server))
    self._send(200, page.encode(), "text/html; charset=utf-8")


_server.Handler._serve_index = _serve_embedded

if __name__ == "__main__":
    _sys.exit(_sys.modules["procwatch.cli"].main())
'''


def build(out_path):
    parts = [HEADER]
    for name in MODULES:
        path = os.path.join(HERE, "procwatch", name + ".py")
        with open(path) as handle:
            source = handle.read()
        parts.append("_%s = _install(%r, %r)\n" % (name, name, source))

    # The menu bar app's sources travel with the bundle, so a machine that
    # only ever ran the one-line installer can still build it. Compiling
    # locally is what keeps the result out of quarantine -- a downloaded
    # unsigned bundle is refused outright by Gatekeeper.
    parts.append("\n_appbuild = _sys.modules['procwatch.appbuild']\n")
    # A 512px master rather than the .icns, which carries every size up to
    # 1024@2x and weighs 2.2 MB on its own -- most of a file people are asked
    # to curl. The icon set is generated from this at build time.
    sources = {"ProcwatchBar.swift": os.path.join(HERE, "menubar", "ProcwatchBar.swift"),
               "icon.png": os.path.join(HERE, "menubar", "icon.png"),
               "procwatch-bar.png": os.path.join(HERE, "menubar", "procwatch-bar.png")}
    for name, path in sources.items():
        with open(path, "rb") as handle:
            blob = base64.b64encode(handle.read()).decode("ascii")
        chunks = [blob[i:i + 76] for i in range(0, len(blob), 76)]
        parts.append("_appbuild.EMBEDDED[%r] = (\n" % name)
        parts.extend('    "%s"\n' % chunk for chunk in chunks)
        parts.append(")\n")

    for name, var in (("index.html", "_INDEX_HTML_B64"),
                      ("netmonitor.html", "_NETMON_HTML_B64"),
                      ("storage.html", "_STORAGE_HTML_B64"),
                      ("battery.html", "_BATTERY_HTML_B64"),
                      ("world.js", "_WORLD_JS_B64")):
        page = os.path.join(HERE, "procwatch", "static", name)
        with open(page, "rb") as handle:
            encoded = base64.b64encode(handle.read()).decode("ascii")
        # Wrapped so the line does not run to tens of thousands of characters.
        chunks = [encoded[i:i + 76] for i in range(0, len(encoded), 76)]
        parts.append("\n%s = (\n" % var)
        parts.extend('    "%s"\n' % chunk for chunk in chunks)
        parts.append(")\n")
    parts.append(FOOTER)

    with open(out_path, "w") as handle:
        handle.write("".join(parts))
    os.chmod(out_path, 0o755)
    return out_path


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "procwatch.py")
    path = build(target)
    print("wrote %s (%.0f KB)" % (path, os.path.getsize(path) / 1024.0))
