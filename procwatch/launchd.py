"""Generate and load the launchd agent."""
import os
import plistlib
import subprocess
import sys

from . import config

LABEL = "dev.procwatch.sampler"
PLIST_PATH = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)


def entry_point():
    """How launchd should re-invoke this code, and from where.

    Two shapes have to work. From a checkout, `-m procwatch.main` resolves
    against the package directory. From the single-file build there is no
    package directory -- `__path__` is empty -- and `-m` would resolve to
    nothing, so the agent is pointed at the script itself.

    Getting this wrong is silent: launchd accepts the job, runs it every 30
    seconds, and every run fails. So an unusable answer raises instead.
    """
    package = sys.modules.get(__package__ or "procwatch")
    bundled = not getattr(package, "__path__", None)
    if bundled:
        script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
        if not (script.endswith(".py") and os.path.isfile(script)):
            raise RuntimeError(
                "cannot locate procwatch.py to schedule; run the installer, or "
                "invoke it as `python3 /full/path/to/procwatch.py install`")
        return [sys.executable, script, "record"], os.path.dirname(script)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [sys.executable, "-m", "procwatch.main"], root


def plist_text(arguments, working_dir):
    payload = {
        "Label": LABEL,
        "ProgramArguments": arguments,
        "StartInterval": config.INTERVAL,
        "RunAtLoad": True,
        "KeepAlive": False,
        "WorkingDirectory": working_dir,
        "StandardErrorPath": config.LOG_PATH,
    }
    return plistlib.dumps(payload).decode()


def install():
    config.ensure_dirs()
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    arguments, working_dir = entry_point()
    with open(PLIST_PATH, "w") as handle:
        handle.write(plist_text(arguments, working_dir))
    subprocess.run(["launchctl", "unload", PLIST_PATH], capture_output=True)
    subprocess.run(["launchctl", "load", PLIST_PATH], check=True)
    return PLIST_PATH


def uninstall():
    subprocess.run(["launchctl", "unload", PLIST_PATH], capture_output=True)
    if os.path.exists(PLIST_PATH):
        os.remove(PLIST_PATH)
