"""The battery's own condition: how worn it is, how hard it is working, and
how long it has left.

Read from AppleSmartBattery through ioreg, which needs no privilege and
answers in about 20 ms. `pmset -g batt` is the better-known source and is not
enough: it reports the charge and nothing about wear, and wear is the question
people actually have about a battery that is three years old.

HEALTH IS NOMINAL OVER DESIGN, NOT "MaxCapacity". On Apple silicon MaxCapacity
reports 100 forever -- it is a percentage of the current full charge, not of
the original one -- so a tool that reads it tells everybody their battery is
perfect. NominalChargeCapacity against DesignCapacity is the real comparison,
and it is the number System Information shows.

Everything here is a snapshot. The history of charge over time already lives
in system_raw, recorded by the sampler every tick.
"""
import re
import subprocess

IOREG = ["ioreg", "-r", "-c", "AppleSmartBattery", "-w0"]

# Below this Apple calls a battery "Service Recommended" in System Information.
WORN = 80.0
# The cycle rating for every current Mac laptop. Kept as a fallback for the
# rare machine that does not publish DesignCycleCount9C.
RATED_CYCLES = 1000


def _read_ioreg():
    try:
        done = subprocess.run(IOREG, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout if done.returncode == 0 else ""


def _number(text, key):
    """One integer out of ioreg's flat dump.

    The output is not JSON and nested dictionaries are printed inline, so a
    key can appear inside a larger value. Anchoring on the quoted key and
    taking only the digits that immediately follow is what keeps
    "DesignCapacity"=4563,"CurrentCapacity"=80 from reading as 456380.
    """
    found = re.search(r'"%s"\s*=\s*(-?\d+)' % re.escape(key), text)
    return int(found.group(1)) if found else None


def _flag(text, key):
    found = re.search(r'"%s"\s*=\s*(Yes|No|true|false|\d+)' % re.escape(key), text)
    if not found:
        return None
    value = found.group(1)
    if value in ("Yes", "true"):
        return True
    if value in ("No", "false"):
        return False
    return value != "0"


def _text(text, key):
    found = re.search(r'"%s"\s*=\s*"([^"]*)"' % re.escape(key), text)
    return found.group(1) if found else ""


def parse(text):
    """Everything worth saying about the battery, from one ioreg dump."""
    if not text or _flag(text, "BatteryInstalled") is False:
        return {"present": False}

    design = _number(text, "DesignCapacity")
    nominal = _number(text, "NominalChargeCapacity")
    cycles = _number(text, "CycleCount")
    rated = _number(text, "DesignCycleCount9C") or RATED_CYCLES
    charging = bool(_flag(text, "IsCharging"))
    plugged = bool(_flag(text, "ExternalConnected"))

    health = None
    if design and nominal:
        health = round(nominal / float(design) * 100, 1)

    # ioreg reports minutes, and 65535 is its "cannot say yet" -- which is
    # what it returns for the direction the battery is not currently going.
    def minutes(key):
        value = _number(text, key)
        return value if value is not None and 0 < value < 65535 else None

    # Tenths of a degree, and only on some machines.
    temp = _number(text, "Temperature")
    amps = _number(text, "InstantAmperage")
    volts = _number(text, "Voltage")
    # A 16-bit counter reported unsigned: anything above half the range is a
    # negative current, which is the battery discharging.
    if amps is not None and amps > 2 ** 31:
        amps -= 2 ** 32

    return {
        "present": True,
        "percent": _number(text, "CurrentCapacity"),
        "charging": charging,
        "plugged": plugged,
        "fully_charged": bool(_flag(text, "FullyCharged")),
        "cycles": cycles,
        "rated_cycles": rated,
        "cycles_left": (rated - cycles) if (cycles is not None) else None,
        "design_mah": design,
        "nominal_mah": nominal,
        "health": health,
        "worn": (health is not None and health < WORN),
        "serial": _text(text, "Serial"),
        "volts": round(volts / 1000.0, 2) if volts else None,
        "amps": round(amps / 1000.0, 2) if amps else None,
        "watts": (round(abs(amps) * volts / 1e6, 1)
                  if (amps and volts) else None),
        "celsius": round(temp / 100.0, 1) if temp else None,
        "to_full_minutes": minutes("AvgTimeToFull") if charging else None,
        "to_empty_minutes": minutes("AvgTimeToEmpty") if not plugged else None,
    }


def read():
    return parse(_read_ioreg())


def verdict(state):
    """One sentence about whether anything needs doing.

    A panel of numbers with no reading is the thing this project exists not to
    ship: "3920 of 4563 mAh" is a fact, "this battery has lost 14% of its
    original capacity, which is normal for 179 cycles" is an answer.
    """
    if not state.get("present"):
        return "No battery is installed."
    health, cycles = state.get("health"), state.get("cycles")
    if health is None:
        return "macOS is not reporting this battery's capacity."
    lost = round(100 - health, 1)
    if state.get("worn"):
        return ("This battery holds %.0f%% of what it did new. Below 80%% is "
                "when Apple calls it Service Recommended, so it is worth "
                "replacing." % health)
    if cycles is not None and cycles < 100 and lost < 5:
        return "This battery is effectively new: %.0f%% of its original capacity." % health
    return ("This battery holds %.0f%% of what it did new, which is normal "
            "wear for %s cycles." % (health, cycles if cycles is not None else "its"))
