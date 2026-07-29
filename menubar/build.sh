#!/bin/sh
# Build the menu bar app. System frameworks only -- no packages, no Xcode
# project, just swiftc and a hand-written bundle.
set -e
cd "$(dirname "$0")"
APP="ProcwatchBar.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp procwatch.icns "$APP/Contents/Resources/procwatch.icns"
cp procwatch-bar.png "$APP/Contents/Resources/procwatch-bar.png"

# The app carries the whole tool, so it runs from wherever it is dragged
# rather than out of a checkout that happens to be on the build machine.
python3 ../tools/bundle.py ../procwatch.py >/dev/null
cp ../procwatch.py "$APP/Contents/Resources/procwatch.py"

swiftc -O ProcwatchBar.swift -o "$APP/Contents/MacOS/ProcwatchBar" \
  -framework AppKit -framework WebKit

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>procwatch</string>
  <key>CFBundleIdentifier</key><string>dev.procwatch.bar</string>
  <key>CFBundleExecutable</key><string>ProcwatchBar</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleIconFile</key><string>procwatch</string>
  <key>LSUIElement</key><true/>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST

# Install into /Applications so it shows up in Launchpad, Spotlight and the
# app switcher like anything else. A running copy is quit first: replacing the
# bundle underneath a live process leaves it running from a deleted image.
DEST="/Applications/$APP"
if [ "${PROCWATCH_NO_INSTALL:-}" = "1" ]; then
  echo "built $(pwd)/$APP (not installed)"
  exit 0
fi
osascript -e 'quit app "procwatch"' 2>/dev/null || true
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
