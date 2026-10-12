"""Being a good citizen on battery: power source, Low Power Mode, thread QoS."""
import ctypes
import re
import subprocess
import sys

# pthread QoS classes from <sys/qos.h>
QOS_BACKGROUND = 0x09     # efficiency cores, throttled disk: scheduled scans
QOS_UTILITY = 0x11        # work you started and are waiting on


def _pmset(*args):
    try:
        r = subprocess.run(["pmset"] + list(args), capture_output=True, text=True, timeout=5)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def parse_on_battery(batt_output):
    """`pmset -g batt` starts "Now drawing from 'Battery Power'" or "'AC Power'"."""
    m = re.search(r"drawing from '([^']+)'", batt_output or "")
    return bool(m) and m.group(1) == "Battery Power"


def parse_low_power(g_output):
    """`pmset -g` lists "lowpowermode 1" when Low Power Mode is on."""
    return bool(re.search(r"^\s*lowpowermode\s+1\b", g_output or "", re.M))


def on_battery():
    return sys.platform == "darwin" and parse_on_battery(_pmset("-g", "batt"))


def low_power_mode():
    return sys.platform == "darwin" and parse_low_power(_pmset("-g"))


def should_defer():
    """Optional heavy work waits for the charger and normal power mode."""
    return on_battery() or low_power_mode()


def set_thread_qos(qos):
    """Lower (or raise) the calling thread's QoS on macOS. No-op elsewhere."""
    if sys.platform != "darwin":
        return False
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        fn = libc.pthread_set_qos_class_self_np
        fn.argtypes = [ctypes.c_uint, ctypes.c_int]
        fn.restype = ctypes.c_int
        return fn(qos, 0) == 0
    except (OSError, AttributeError):
        return False
