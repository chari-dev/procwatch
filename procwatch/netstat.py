"""Per-process network bytes, and what is listening on which port.

macOS has no per-process network counter in `ps`. `nettop` has one and runs
without sudo, so that is what this parses. Its `-L 1` mode prints one CSV
sample and exits, which suits a stateless tick.

Like the disk counters, `nettop`'s bytes are cumulative for the life of the
process, so rates come from differencing consecutive ticks.
"""
import re
import subprocess

# -n is load-bearing, not cosmetic. Without it nettop spends 5.0 seconds of
# every run resolving peer names, and -x alone does not stop it: -x asks for
# numeric *display*, -n is what suppresses the lookups. Measured on this
# machine, three alternating runs: 5.03s letting it resolve, 0.02s with -n,
# byte-identical pid sets both ways. That five seconds was the whole tick --
# the recorder was landing every 36s against a configured INTERVAL of 30, so
# every duration derived from INTERVAL read 20% short.
NETTOP = ["nettop", "-P", "-x", "-n", "-L", "1", "-J", "bytes_in,bytes_out"]
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


# Without -P nettop prints each process row followed by one row per socket:
#   apsd.369,9215754,30461619,
#   tcp4 192.168.4.50:56611<->17.188.169.70:5223,9215754,30461619,
# -x keeps addresses numeric and -n stops the lookups behind them. Both are
# needed: -x alone still resolves, it just prints the number afterwards, and
# resolving every peer inline is what nettop spends its slow seconds on.
_CONN = re.compile(r"^(?P<proto>tcp[46]?|udp[46]?)\s+"
                   r"(?P<local>\S+)<->(?P<remote>\S+)$")


def split_endpoint(text):
    """("17.188.169.70", "5223") out of nettop's address cell.

    v4 rows join host and port with ":", v6 rows with "." -- and a v6 host
    is itself full of ":", so the joiner is identified by what the host
    looks like rather than assumed: more than one ":" means v6, and the
    port hangs off the last ".".
    """
    if not text or text.startswith("*"):
        return text, ""
    if text.count(":") > 1:
        host, _, port = text.rpartition(".")
    else:
        host, _, port = text.rpartition(":")
    return host, port


def parse_traffic(text):
    """Per-process totals with their live connections, from nettop's
    connection-level CSV: [{pid, name, bytes_in, bytes_out, conns}], where
    each conn is {proto, local, remote, bytes_in, bytes_out}.

    Wildcard peers -- listeners and unconnected sockets -- are left out:
    they are not traffic, and the listening ports have their own panel.
    """
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return []
    header = [h.strip() for h in lines[0].split(",")]
    try:
        i_in = header.index("bytes_in")
        i_out = header.index("bytes_out")
    except ValueError:
        return []

    def number(cell):
        try:
            return int(cell)
        except ValueError:
            return 0

    out, current = [], None
    for line in lines[1:]:
        cells = line.split(",")
        if len(cells) <= max(i_in, i_out):
            continue
        name_cell = cells[0].strip()
        proc = _ROW.match(name_cell)
        conn = _CONN.match(name_cell)
        if proc and not conn:
            current = {"pid": int(proc.group("pid")),
                       "name": proc.group("name"),
                       "bytes_in": number(cells[i_in]),
                       "bytes_out": number(cells[i_out]),
                       "conns": []}
            out.append(current)
            continue
        if conn and current is not None:
            remote = conn.group("remote")
            if remote.startswith("*"):
                continue
            host, port = split_endpoint(remote)
            current["conns"].append({
                "proto": conn.group("proto"),
                "local": conn.group("local"),
                "remote": remote, "host": host, "port": port,
                "bytes_in": number(cells[i_in]),
                "bytes_out": number(cells[i_out])})
    return out


def traffic():
    """Every process with a live network connection, and each connection's
    peer and lifetime bytes. Empty list when nettop is unavailable."""
    try:
        done = subprocess.run(["nettop", "-x", "-n", "-L", "1",
                               "-J", "bytes_in,bytes_out"],
                              capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if done.returncode != 0:
        return []
    return parse_traffic(done.stdout)


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
