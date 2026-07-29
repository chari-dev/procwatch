"""System-wide readings that sit alongside the per-process rows."""
import collections
import os
import re
import subprocess

from . import config


class SystemReadError(Exception):
    """A system reading could not be obtained or parsed."""


Readings = collections.namedtuple(
    "Readings", "load1 mem_used_kb mem_comp_kb swap_used_kb disk_free_kb")

_PAGE_SIZE = re.compile(r"page size of (\d+) bytes")
_SWAP_USED = re.compile(r"used = ([\d.]+)M")
_BOOT_SEC = re.compile(r"sec = (\d+)")


def _pages(text, label):
    match = re.search(re.escape(label) + r":\s+(\d+)", text)
    if match is None:
        raise SystemReadError("vm_stat has no %r field" % label)
    return int(match.group(1))


def parse_vm_stat(text):
    """Return (used_kb, compressed_kb) from vm_stat output.

    "Used" is active + wired. Compressed pages are deliberately NOT included
    -- they are returned separately as the second element. A caller wanting
    the figure Activity Monitor shows as "Memory Used" must add the two.
    """
    page_match = _PAGE_SIZE.search(text)
    page_kb = (int(page_match.group(1)) if page_match else 4096) // 1024
    used = _pages(text, "Pages active") + _pages(text, "Pages wired down")
    compressed = _pages(text, "Pages occupied by compressor")
    return used * page_kb, compressed * page_kb


def parse_swapusage(text):
    match = _SWAP_USED.search(text)
    if match is None:
        raise SystemReadError("could not read used swap from %r" % text.strip())
    return int(float(match.group(1)) * 1024)


def _run(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        raise SystemReadError("%s failed: %s" % (args[0], result.stderr.strip()))
    return result.stdout


def read():
    load1 = os.getloadavg()[0]
    mem_used_kb, mem_comp_kb = parse_vm_stat(_run(["vm_stat"]))
    swap_used_kb = parse_swapusage(_run(["sysctl", "vm.swapusage"]))
    stat = os.statvfs(os.path.expanduser("~"))
    disk_free_kb = stat.f_bavail * stat.f_frsize // 1024
    return Readings(int(round(load1 * 100)), mem_used_kb, mem_comp_kb,
                    swap_used_kb, disk_free_kb)


def free_bytes():
    stat = os.statvfs(os.path.dirname(config.DB_PATH))
    return stat.f_bavail * stat.f_frsize


def machine_info():
    """Facts the dashboard header needs and no tick loop does: hostname, OS
    version, chip name, core count, total RAM, and boot time. Read fresh on
    each request rather than cached -- this endpoint is hit once per page
    load, not once per 30-second tick, so the extra subprocess calls are
    cheap next to a full sampling pass.
    """
    hostname = os.uname().nodename.split(".")[0]
    os_version = _run(["sw_vers", "-productVersion"]).strip()
    chip = _run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip()
    cores = os.cpu_count() or 1
    mem_total_kb = int(_run(["sysctl", "-n", "hw.memsize"]).strip()) // 1024
    boot_match = _BOOT_SEC.search(_run(["sysctl", "-n", "kern.boottime"]))
    boot_ts = int(boot_match.group(1)) if boot_match else None
    stat = os.statvfs(os.path.expanduser("~"))
    disk_total_kb = stat.f_blocks * stat.f_frsize // 1024
    return {"hostname": hostname, "os_version": os_version, "chip": chip,
            "cores": cores, "mem_total_kb": mem_total_kb, "boot_ts": boot_ts,
            "disk_total_kb": disk_total_kb}


_IOREG_INT = "\"%s\"\\s*=\\s*(-?\\d+)"


def _ioreg_battery():
    try:
        return _run(["ioreg", "-rc", "AppleSmartBattery"])
    except Exception:
        return ""


def parse_battery(text):
    """Battery state from ioreg output.

    Returns percent (0-100), whether it is on wall power, the instantaneous
    draw in milliwatts, and what a full charge holds in milliwatt-hours.

    On wall power the battery reports zero amperage, so there is no drain to
    measure and watts comes back None rather than 0 -- those mean different
    things and conflating them would let the dashboard claim a machine
    plugged in all day cost no energy.
    """
    def find(key):
        match = re.search(r'"%s"\s*=\s*(-?\d+)' % key, text)
        return int(match.group(1)) if match else None

    percent = find("CurrentCapacity")
    voltage_mv = find("Voltage")
    amperage = find("InstantAmperage")
    if amperage is None:
        amperage = find("Amperage")
    if amperage is not None and amperage > 2 ** 63:
        amperage -= 2 ** 64          # reported unsigned; discharge is negative
    on_ac = '"ExternalConnected" = Yes' in text
    full_mah = find("FullChargeCapacity")

    watts_mw = None
    if voltage_mv and amperage:
        watts_mw = abs(voltage_mv * amperage) // 1000
    full_mwh = (voltage_mv * full_mah) // 1000 if (voltage_mv and full_mah) else None
    return {"percent": percent, "on_ac": on_ac,
            "draw_mw": watts_mw, "full_mwh": full_mwh}


def battery():
    return parse_battery(_ioreg_battery())
