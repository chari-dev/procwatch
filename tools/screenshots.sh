#!/bin/sh
# Capture the README screenshots.
#
# Needs Screen Recording permission for whichever terminal runs it:
# System Settings > Privacy & Security > Screen Recording.
set -e
cd "$(dirname "$0")/.."
mkdir -p docs/images
PORT="${1:-8790}"

python3 -m procwatch.server_only "$PORT" >/dev/null 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null' EXIT
sleep 3

open -a Safari "http://127.0.0.1:$PORT/"
echo "Waiting 8s for the dashboard to draw..."
sleep 8

BOUNDS=$(osascript -e 'tell application "Safari" to get bounds of front window')
python3 - "$BOUNDS" <<'PY'
import subprocess, sys
x1, y1, x2, y2 = [int(v.strip()) for v in sys.argv[1].split(",")]
subprocess.run(["screencapture", "-o", "-x", "-R",
                "%d,%d,%d,%d" % (x1, y1, x2 - x1, y2 - y1),
                "docs/images/dashboard.png"], check=True)
PY
echo "wrote docs/images/dashboard.png"

echo "Now open the menu bar panel, then press Return here."
read -r _
screencapture -o -x docs/images/menubar.png
echo "wrote docs/images/menubar.png"
