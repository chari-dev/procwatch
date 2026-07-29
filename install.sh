#!/bin/sh
# procwatch installer.
#
# Installs three things, each of which works without the other two:
#
#   1. the tool itself, as one Python file under ~/Library/Application Support
#   2. a launchd agent that records a sample every 30 seconds
#   3. the menu bar app, if the machine can build it
#
# Deliberately not a .pkg: a pkg needs a signing identity to install without
# a Gatekeeper fight, and everything here goes into the user's own home
# directory, so nothing needs root. Run it again any time to upgrade in place.
#
#   sh install.sh            install or upgrade
#   sh install.sh --no-app   skip the menu bar app
#   sh install.sh --uninstall
set -eu

APP_SUPPORT="$HOME/Library/Application Support/procwatch"
TARGET="$APP_SUPPORT/procwatch.py"
APP="/Applications/Procwatch.app"
PLIST="$HOME/Library/LaunchAgents/dev.procwatch.sampler.plist"
DATA="$HOME/.local/share/procwatch"
HERE=$(cd "$(dirname "$0")" && pwd)
REPO="https://github.com/chari-dev/procwatch"
RAW="https://raw.githubusercontent.com/chari-dev/procwatch/main"

say()  { printf '  %s\n' "$*"; }
fail() { printf '\nprocwatch: %s\n' "$*" >&2; exit 1; }

uninstall() {
    printf '\nRemoving Procwatch\n'
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST" && say "recorder stopped"
    osascript -e 'quit app "Procwatch"' 2>/dev/null || true
    pkill -f "ProcwatchBar" 2>/dev/null || true
    rm -rf "$APP" && say "menu bar app removed"
    # 1.0.0 installed the bundle as ProcwatchBar.app. An upgrade would
    # otherwise leave a stale second copy in /Applications and Launchpad.
    rm -rf "/Applications/ProcwatchBar.app"
    rm -rf "$APP_SUPPORT" && say "program removed"
    # The sampler log is diagnostics, not history, so it goes with the program.
    rm -rf "$HOME/.local/state/procwatch" && say "logs removed"
    # The recording is the point of the tool and may represent months of
    # history, so it is never deleted without being asked for.
    if [ -e "$DATA/procwatch.db" ]; then
        printf '\nYour history is kept at %s\n' "$DATA/procwatch.db"
        printf 'Delete it yourself if you want it gone:\n  rm -rf %s\n' "$DATA"
    fi
    exit 0
}

WANT_APP=yes
for arg in "$@"; do
    case "$arg" in
        --uninstall) uninstall ;;
        --no-app)    WANT_APP=no ;;
        *)           fail "unknown option: $arg" ;;
    esac
done

[ "$(uname -s)" = "Darwin" ] || fail "this only runs on macOS"
command -v python3 >/dev/null 2>&1 || fail \
    "python3 not found. Install the Command Line Tools with: xcode-select --install"
python3 - <<'PY' || fail "python 3.9 or newer is required"
import sys
sys.exit(0 if sys.version_info >= (3, 9) else 1)
PY

printf '\nInstalling Procwatch\n'

# 1. The program. Built from source in a checkout; used as-is when someone has
#    downloaded only the single file.
mkdir -p "$APP_SUPPORT"
if [ -f "$HERE/tools/bundle.py" ]; then
    python3 "$HERE/tools/bundle.py" "$TARGET" >/dev/null
    say "built from source"
elif [ -f "$HERE/procwatch.py" ]; then
    cp "$HERE/procwatch.py" "$TARGET"
    say "copied procwatch.py"
else
    # Piped from the web: nothing is beside this script, so fetch the one
    # file the tool actually consists of. The release asset is preferred
    # because it is a fixed version; main is the fallback while a release is
    # being cut.
    say "downloading procwatch"
    if ! curl -fsSL "$REPO/releases/latest/download/procwatch.py" -o "$TARGET" &&
       ! curl -fsSL "$RAW/procwatch.py" -o "$TARGET"; then
        fail "could not download procwatch.py from $REPO"
    fi
    # A download that returns a 404 page still exits 0 under some proxies.
    head -n 1 "$TARGET" | grep -q "^#!" || fail "downloaded file is not procwatch.py"
fi
chmod +x "$TARGET"
say "installed $TARGET"

# 2. The recorder. Its plist names this interpreter and this path, so
#    reinstalling after a Python upgrade is the fix if it ever stops.
python3 "$TARGET" install >/dev/null
say "recording every 30 seconds"

# 3. The menu bar app. Optional, and the only part needing developer tools --
#    a machine without them still gets a working recorder and dashboard.
if [ "$WANT_APP" = yes ]; then
    if ! command -v swiftc >/dev/null 2>&1; then
        say "skipped the menu bar app (no swiftc -- run: xcode-select --install,"
        say "then: python3 \"$TARGET\" app)"
    elif [ -f "$HERE/menubar/build.sh" ]; then
        sh "$HERE/menubar/build.sh" >/dev/null 2>&1 && say "menu bar app installed"
        pkill -f "Procwatch.app/Contents/MacOS" 2>/dev/null || true
        sleep 1
        open -a "$APP" 2>/dev/null || true
    else
        # No checkout: the program carries the app's sources and builds it
        # here. Compiling on this machine is what keeps the result out of
        # quarantine -- a downloaded unsigned bundle is refused outright.
        if python3 "$TARGET" app >/dev/null 2>&1; then
            say "menu bar app built and installed"
        else
            say "could not build the menu bar app; the dashboard still works"
            say "  try: python3 \"$TARGET\" app"
        fi
    fi
fi

cat <<EOF

Done. The recorder is running and will keep running across restarts.

  Dashboard      python3 "$TARGET" open
  What is stored python3 "$TARGET" status
  Remove it      sh $HERE/install.sh --uninstall

Charts fill in as samples accumulate; give it a few minutes.
EOF
