"""Per-process network bytes, and what is listening on which port.

macOS has no per-process network counter in `ps`. `nettop` has one and runs
without sudo, so that is what this parses. Its `-L 1` mode prints one CSV
sample and exits, which suits a stateless tick.

Like the disk counters, `nettop`'s bytes are cumulative for the life of the
process, so rates come from differencing consecutive ticks.
"""
import re
import subprocess

NETTOP = ["nettop", "-P", "-x", "-L", "1", "-J", "bytes_in,bytes_out"]
LSOF = ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]

# nettop names a row "<name>.<pid>", and the name itself may contain dots.
_ROW = re.compile(r"^(?P<name>.+)\.(?P<pid>\d+)$")


def parse_nettop(text):
    """{pid: (bytes_in, bytes_out)} from nettop's CSV output.

    The header names the columns, so their positions are read from it rather
    than assumed -- nettop's column set differs between macOS releases.
    """
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return {}
    header = [h.strip() for h in lines[0].split(",")]
    try:
        i_in = header.index("bytes_in")
        i_out = header.index("bytes_out")
    except ValueError:
        return {}

    out = {}
    for line in lines[1:]:
        cells = line.split(",")
        if len(cells) <= max(i_in, i_out):
            continue
        # The "<name>.<pid>" cell sits at index 0 with -J and index 1 without
        # it, because the plain form leads with a timestamp. Find it rather
        # than assume, so both layouts parse.
        match = None
        for cell in cells[:2]:
            match = _ROW.match(cell.strip())
            if match:
                break
        if not match:
            continue
        try:
            pid = int(match.group("pid"))
            got_in = int(cells[i_in] or 0)
            got_out = int(cells[i_out] or 0)
        except ValueError:
            continue
        # nettop can list a pid more than once (per interface); sum them.
        prev = out.get(pid, (0, 0))
        out[pid] = (prev[0] + got_in, prev[1] + got_out)
    return out


def read():
    """{pid: (bytes_in, bytes_out)}. Empty dict if nettop is unavailable."""
    try:
        done = subprocess.run(NETTOP, capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if done.returncode != 0:
        return {}
    return parse_nettop(done.stdout)


def parse_listeners(text):
    """[{pid, command, user, port, proto, address}] from lsof output."""
    rows = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        name = parts[-2] if parts[-1] == "(LISTEN)" else parts[-1]
        if ":" not in name:
            continue
        address, _, port = name.rpartition(":")
        try:
            port_num = int(port)
            pid = int(parts[1])
        except ValueError:
            continue
        rows.append({"pid": pid, "command": parts[0], "user": parts[2],
                     "proto": parts[7], "address": address or "*",
                     "port": port_num})
    return rows


def listeners():
    """Everything currently listening on a TCP port.

    Deduplicated by (pid, port): lsof prints a row per socket, so a process
    bound on both IPv4 and IPv6 would otherwise appear twice.
    """
    try:
        done = subprocess.run(LSOF, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    seen, out = set(), []
    for row in parse_listeners(done.stdout):
        key = (row["pid"], row["port"])
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    out.sort(key=lambda r: r["port"])
    return out
