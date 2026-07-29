"""Serve the dashboard without opening a browser or timing out.

The menu bar app launches this and owns its lifetime, so the idle reaper that
suits `procwatch open` from a terminal would be wrong here -- the panel being
closed is not a reason to stop serving.
"""
import sys

from . import server


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8790
    return server.serve(port, open_browser=False, idle_timeout=None)


if __name__ == "__main__":
    sys.exit(main())
