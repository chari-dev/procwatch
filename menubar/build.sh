#!/bin/sh
# Build the menu bar app. System frameworks only -- no packages, no Xcode
# project, just swiftc and a hand-written bundle.
set -e
cd "$(dirname "$0")"
APP="Procwatch.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp procwatch.icns "$APP/Contents/Resources/procwatch.icns"
cp procwatch-bar.png "$APP/Contents/Resources/procwatch-bar.png"

# The app carries the whole tool, so it runs from wherever it is dragged
# rather than out of a checkout that happens to be on the build machine.
python3 ../tools/bundle.py ../procwatch.py >/dev/null
cp ../procwatch.py "$APP/Contents/Resources/procwatch.py"

# The version, from the one place it is written down, and a build identifier
# derived from what was actually built. Hardcoding the version here is how the
# bundle came to claim 1.0 through four releases -- and how the tool that
# reports on application updates stayed blind to its own.
VERSION=$(python3 -c 'import sys; sys.path.insert(0, ".."); from procwatch import config; print(config.VERSION)')
BUILD=$(shasum -a 256 ../procwatch.py | cut -c1-8)

swiftc -O ProcwatchBar.swift -o "$APP/Contents/MacOS/ProcwatchBar" \
  -framework AppKit -framework WebKit

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Procwatch</string>
  <key>CFBundleIdentifier</key><string>dev.procwatch.bar</string>
  <key>CFBundleExecutable</key><string>ProcwatchBar</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>__VERSION__</string>
  <key>CFBundleVersion</key><string>__BUILD__</string>
  <key>CFBundleIconFile</key><string>procwatch</string>
  <key>LSUIElement</key><true/>
  <key>NSLocalNetworkUsageDescription</key>
  <string>Procwatch reads the history of other Macs you have added, on your local network.</string>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST

# Substituted rather than interpolated: the heredoc stays quoted so nothing in
# the plist's prose can expand, and only these two placeholders move.
sed -i '' -e "s/__VERSION__/$VERSION/" -e "s/__BUILD__/$BUILD/" \
    "$APP/Contents/Info.plist"

# swiftc leaves a linker signature that covers the binary and not the bundle,
# so Info.plist is "not bound" -- macOS then has no usage description to show
# and no stable identity to remember a decision against, which is why the app
# never appeared under Local Network. An ad-hoc signature over the bundle
# fixes both. No certificate is needed.
codesign --force --sign - --identifier dev.procwatch.bar "$APP" >/dev/null 2>&1 || true

# Install into /Applications so it shows up in Launchpad, Spotlight and the
# app switcher like anything else. A running copy is quit first: replacing the
# bundle underneath a live process leaves it running from a deleted image.
DEST="/Applications/$APP"
# The bundle was called ProcwatchBar.app in 1.0.0. Leaving it behind would put
# two apps with the same icon in Launchpad, one of them stale.
rm -rf "/Applications/ProcwatchBar.app" 2>/dev/null || true
if [ "${PROCWATCH_NO_INSTALL:-}" = "1" ]; then
  echo "built $(pwd)/$APP (not installed)"
  exit 0
fi
osascript -e 'quit app "Procwatch"' 2>/dev/null || true
pkill -f "$APP/Contents/MacOS/ProcwatchBar" 2>/dev/null || true
if rm -rf "$DEST" 2>/dev/null && cp -R "$APP" "$DEST" 2>/dev/null; then
  # Clear the icon cache entry for the new bundle, otherwise Finder keeps
  # showing whatever art it saw at this path first.
  touch "$DEST"
  echo "installed $DEST"
else
  echo "built $(pwd)/$APP"
  echo "could not write to /Applications -- copy it there yourself, or:"
  echo "  sudo cp -R $(pwd)/$APP /Applications/"
fi
