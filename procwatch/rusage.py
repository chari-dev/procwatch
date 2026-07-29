"""Per-process disk, energy and memory counters from libproc.

`ps` does not report disk I/O or energy at all, and Activity Monitor's numbers
come from `proc_pid_rusage`, which is what this wraps. No sudo is required for
processes owned by the calling user; other users' processes simply return None
rather than raising, so a mixed-ownership listing degrades to partial coverage
instead of failing.

Every counter here is CUMULATIVE since process start, exactly like `cputime`.
Rates come from differencing consecutive ticks, never from the raw value.
"""
import ctypes
import ctypes.util

RUSAGE_INFO_V4 = 4

_lib = None


class _RUsageV4(ctypes.Structure):
    """<sys/resource.h> struct rusage_info_v4, field for field.

    The layout is positional, so a missing or reordered field silently
    misreads every counter after it. It is written out in full rather than
    indexed by offset for that reason.
    """
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
        ("ri_child_user_time", ctypes.c_uint64),
        ("ri_child_system_time", ctypes.c_uint64),
        ("ri_child_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_child_interrupt_wkups", ctypes.c_uint64),
        ("ri_child_pageins", ctypes.c_uint64),
        ("ri_child_elapsed_abstime", ctypes.c_uint64),
        ("ri_diskio_bytesread", ctypes.c_uint64),
        ("ri_diskio_byteswritten", ctypes.c_uint64),
        ("ri_cpu_time_qos_default", ctypes.c_uint64),
        ("ri_cpu_time_qos_maintenance", ctypes.c_uint64),
        ("ri_cpu_time_qos_background", ctypes.c_uint64),
        ("ri_cpu_time_qos_utility", ctypes.c_uint64),
        ("ri_cpu_time_qos_legacy", ctypes.c_uint64),
        ("ri_cpu_time_qos_user_initiated", ctypes.c_uint64),
        ("ri_cpu_time_qos_user_interactive", ctypes.c_uint64),
        ("ri_billed_system_time", ctypes.c_uint64),
        ("ri_serviced_system_time", ctypes.c_uint64),
        ("ri_logical_writes", ctypes.c_uint64),
        ("ri_lifetime_max_phys_footprint", ctypes.c_uint64),
        ("ri_instructions", ctypes.c_uint64),
        ("ri_cycles", ctypes.c_uint64),
        ("ri_billed_energy", ctypes.c_uint64),
        ("ri_serviced_energy", ctypes.c_uint64),
        ("ri_interval_max_phys_footprint", ctypes.c_uint64),
        ("ri_runnable_time", ctypes.c_uint64),
    ]


def _libproc():
    global _lib
    if _lib is None:
        path = ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib"
        _lib = ctypes.CDLL(path)
        _lib.proc_pid_rusage.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.POINTER(_RUsageV4)]
        _lib.proc_pid_rusage.restype = ctypes.c_int
    return _lib


def counters(pid):
    """Cumulative counters for `pid`, or None if it is not readable.

    Returns a tuple of (disk_read, disk_written, logical_written, footprint,
    energy, cycles). Not readable means another user's process or one that
    exited between listing and reading -- both are ordinary, not errors.
    """
    info = _RUsageV4()
    try:
        if _libproc().proc_pid_rusage(pid, RUSAGE_INFO_V4, ctypes.byref(info)) != 0:
            return None
    except OSError:
        return None
    return (info.ri_diskio_bytesread,
            info.ri_diskio_byteswritten,
            info.ri_logical_writes,
            info.ri_phys_footprint,
            info.ri_billed_energy,
            info.ri_cycles)


def read_all(pids):
    """{pid: counters} for every readable pid. Unreadable ones are omitted."""
    out = {}
    for pid in pids:
        got = counters(pid)
        if got is not None:
            out[pid] = got
    return out
