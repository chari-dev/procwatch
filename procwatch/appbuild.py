"""Build and install the menu bar app on the machine that will run it.

The one-line installer fetches two files: this program and the installer. That
left the menu bar app -- the part most people actually look at -- available
only to someone who cloned the repository, which is a strange thing to ask of
someone who has just been told the install is one command.

Attaching a prebuilt .app to each release would be the obvious fix and is the
wrong one: a downloaded bundle that is not signed and notarised is quarantined,
and macOS refuses to open it at all. Compiling on the machine that will run it
produces a bundle with no quarantine attribute, so it simply opens. That is
worth carrying a few kilobytes of Swift for.

The sources live in menubar/ in a checkout and are embedded in the single-file
build, so both forms can do this.
"""
import base64
import os
import shutil
import subprocess
import sys
import tempfile

APP_NAME = "Procwatch.app"
# 1.0.0 and 1.1.x installed it under this name. Left behind, it is a second
# icon in Launchpad running an older copy.
SUPERSEDED = ("ProcwatchBar.app",)

# Filled in by the generated single-file build. In a checkout these stay empty
# and the files are read from menubar/ instead.
EMBEDDED = {}

INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Procwatch</string>
  <key>CFBundleIdentifier</key><string>dev.procwatch.bar</string>
  <key>CFBundleExecutable</key><string>ProcwatchBar</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>__PROCWATCH_VERSION__</string>
  <key>CFBundleIconFile</key><string>procwatch</string>
  <key>LSUIElement</key><true/>
  <key>NSLocalNetworkUsageDescription</key>
  <string>Procwatch reads the history of other Macs you have added, on your local network.</string>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
"""

ASSETS = ("ProcwatchBar.swift", "icon.png", "procwatch-bar.png")

# Every size a bundle icon is asked for. Generated from one master rather than
# shipped as a .icns, which weighs 2.2 MB -- most of a file people are told to
# fetch with curl.
ICON_SIZES = (16, 32, 128, 256, 512)


def _repo_menubar():
    """menubar/ beside the package, when running from a checkout."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "menubar")
    return path if os.path.isdir(path) else None


def asset(name):
    """One build input, as bytes, from wherever this copy keeps them."""
    folder = _repo_menubar()
    if folder:
        path = os.path.join(folder, name)
        if os.path.exists(path):
            with open(path, "rb") as handle:
                return handle.read()
    if name in EMBEDDED:
        return base64.b64decode(EMBEDDED[name])
    raise RuntimeError("missing build input: %s" % name)


def have_swift():
    return shutil.which("swiftc") is not None


def program_path():
    """The installed procwatch.py to put inside the bundle.

    The app runs the server out of its own Resources so it works wherever it
    is dragged; that copy has to come from somewhere. A checkout generates it,
    a bundle is it.
    """
    entry = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if entry.endswith(".py") and os.path.isfile(entry):
        package = sys.modules.get(__package__ or "procwatch")
        if not getattr(package, "__path__", None):
            return entry                      # the single-file build itself
    root = _repo_menubar()
    if root:
        generated = os.path.join(os.path.dirname(root), "procwatch.py")
        if os.path.isfile(generated):
            return generated
    raise RuntimeError(
        "cannot find procwatch.py to place inside the app; run "
        "tools/bundle.py first, or install with the one-line installer")


def build(destination="/Applications"):
    """Compile the app and install it. Returns the installed path."""
    if sys.platform != "darwin":
        raise RuntimeError("the menu bar app is macOS only")
    if not have_swift():
        raise RuntimeError(
            "swiftc not found. Install the Command Line Tools:\n"
            "  xcode-select --install")

    program = program_path()
    work = tempfile.mkdtemp(prefix="procwatch-app-")
    app = os.path.join(work, APP_NAME)
    macos = os.path.join(app, "Contents", "MacOS")
    resources = os.path.join(app, "Contents", "Resources")
    os.makedirs(macos)
    os.makedirs(resources)
    try:
        source = os.path.join(work, "ProcwatchBar.swift")
        with open(source, "wb") as handle:
            handle.write(asset("ProcwatchBar.swift"))
        # IOKit is for the Keep Awake power assertion. It has to be listed
        # here as well as in menubar/build.sh: this is the path the one-line
        # installer takes, and a framework missing from only this copy is a
        # build that works in the checkout and fails on every fresh install.
        result = subprocess.run(
            ["swiftc", "-O", source, "-o", os.path.join(macos, "ProcwatchBar"),
             "-framework", "AppKit", "-framework", "WebKit",
             "-framework", "IOKit"],
            capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError("swiftc failed:\n%s"
                               % (result.stderr or result.stdout).strip()[:800])

        # The declared version is config.VERSION, substituted at build time
        # rather than written in the heredoc. A literal here said 1.0 through
        # four releases (config.py tells the story), and then 1.1.1 through
        # three more -- so procwatch's version panel could not see procwatch's
        # own updates. One source, no drift.
        from . import config
        with open(os.path.join(app, "Contents", "Info.plist"), "w") as handle:
            handle.write(INFO_PLIST.replace("__PROCWATCH_VERSION__",
                                            config.VERSION))
        _write_icon(work, resources)
        with open(os.path.join(resources, "procwatch-bar.png"), "wb") as handle:
            handle.write(asset("procwatch-bar.png"))
        shutil.copy2(program, os.path.join(resources, "procwatch.py"))

        # swiftc's linker signature covers the binary but not the bundle, so
        # Info.plist is unbound: macOS has no usage description to show and no
        # identity to remember a decision against, and the app never appears
        # under Local Network. An ad-hoc signature over the bundle fixes both
        # and needs no certificate.
        try:
            subprocess.run(["codesign", "--force", "--sign", "-",
                            "--identifier", "dev.procwatch.bar", app],
                           capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            pass          # unsigned still runs; it just cannot be granted

        target = os.path.join(destination, APP_NAME)
        _quit_running()
        for old in SUPERSEDED:
            shutil.rmtree(os.path.join(destination, old), ignore_errors=True)
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(app, target)
        return target
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _write_icon(work, resources):
    """Turn the master PNG into procwatch.icns using the tools macOS ships.

    If either tool is missing the app is still built and installed -- it
    simply shows a generic icon in Finder, which is a far better outcome than
    refusing to build over a picture.
    """
    master = os.path.join(work, "icon.png")
    with open(master, "wb") as handle:
        handle.write(asset("icon.png"))
    iconset = os.path.join(work, "procwatch.iconset")
    os.makedirs(iconset, exist_ok=True)
    try:
        for side in ICON_SIZES:
            for scale, suffix in ((1, ""), (2, "@2x")):
                out = os.path.join(iconset, "icon_%dx%d%s.png"
                                   % (side, side, suffix))
                subprocess.run(["sips", "-z", str(side * scale), str(side * scale),
                                master, "--out", out],
                               capture_output=True, timeout=60)
        result = subprocess.run(
            ["iconutil", "-c", "icns", iconset,
             "-o", os.path.join(resources, "procwatch.icns")],
            capture_output=True, timeout=120)
        if result.returncode == 0:
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    # No .icns: the bundle still runs, and the menu bar image is separate.
    shutil.copy2(master, os.path.join(resources, "procwatch.png"))


def _quit_running():
    """Ask a running copy to exit before its bundle is replaced.

    Replacing the bundle underneath a live process leaves it running from a
    directory that no longer exists, which looks like the new build failing to
    take effect.
    """
    for command in (["osascript", "-e", 'quit app "Procwatch"'],
                    ["pkill", "-f", "Procwatch.app/Contents/MacOS/ProcwatchBar"],
                    ["pkill", "-f", "ProcwatchBar.app/Contents/MacOS/ProcwatchBar"]):
        try:
            subprocess.run(command, capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass


def launch(path):
    try:
        subprocess.run(["open", "-a", path], capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        pass
