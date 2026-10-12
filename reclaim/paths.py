"""Where Reclaim keeps its things."""
import os

HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME, ".config", "reclaim")
DATA_DIR = os.path.join(HOME, ".local", "share", "reclaim")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
DB_FILE = os.path.join(DATA_DIR, "reclaim.db")
LOG_FILE = os.path.join(DATA_DIR, "reclaim.log")
TRASH_DIR = os.path.join(DATA_DIR, "trash")
PLIST_LABEL = "com.reclaim.agent"
PLIST_FILE = os.path.join(HOME, "Library", "LaunchAgents", PLIST_LABEL + ".plist")


LOG_MAX_BYTES = 5 * 1024 * 1024


def rotate_log():
    """launchd appends stderr forever; keep one 5 MB generation."""
    try:
        if os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
            os.replace(LOG_FILE, LOG_FILE + ".1")
    except OSError:
        pass


_LOCKS = []


def single_instance(name):
    """Hold a lock for the life of the process; False if another copy has it."""
    import fcntl
    ensure_dirs()
    f = open(os.path.join(DATA_DIR, name + ".lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _LOCKS.append(f)
    return True


def ensure_dirs():
    for d in (CONFIG_DIR, DATA_DIR, TRASH_DIR):
        os.makedirs(d, mode=0o700, exist_ok=True)
    for d in (CONFIG_DIR, DATA_DIR):
        try:
            os.chmod(d, 0o700)      # clipboard history and tokens live here
        except OSError:
            pass
