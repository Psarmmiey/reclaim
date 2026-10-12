"""Deletion, with the brakes on.

Everything goes through `guard()` first. By default items are moved into
Reclaim's own trash so a mistake is recoverable for a few days, rather than
being unlinked immediately.
"""
import os
import shutil
import subprocess
import time

from . import paths, rules, store

MIN_PATH_LEN = 12
TRASH_POLICIES = ("auto", "ask", "always", "never")
_STAMP = "%Y%m%d-%H%M%S"


class Refused(Exception):
    pass


# Folders the user's own files live in. Caches are never deleted from here,
# but a file the user picked by hand in a tool (large, duplicate) may be.
PERSONAL_DIRS = {"Documents", "Desktop", "Movies", "Music", "Pictures"}
LIBRARY_BUNDLES = (".photoslibrary", ".musiclibrary", ".tvlibrary",
                   ".aplibrary", ".imovielibrary", ".fcpbundle")


def guard(path, cfg=None, personal=False):
    """Raise Refused unless this path is genuinely safe to remove.

    personal=True also allows the user's own folders (Documents, Movies...),
    for files they picked by hand. Library bundles such as Photos stay off
    limits either way.
    """
    if not path:
        raise Refused("empty path")
    p = os.path.abspath(os.path.expanduser(path))
    home = os.path.abspath(paths.HOME)

    if os.path.islink(p):
        raise Refused("is a symlink")
    if not os.path.exists(p):
        raise Refused("does not exist")
    if ".." in p.split(os.sep):
        raise Refused("path traversal")
    if len(p) < MIN_PATH_LEN:
        raise Refused("suspiciously short path")
    if p in (home, "/", os.path.dirname(home)):
        raise Refused("refusing to delete a home or root directory")
    if not p.startswith(home + os.sep):
        raise Refused("outside your home folder")
    if p.startswith(paths.DATA_DIR) or p.startswith(paths.CONFIG_DIR):
        raise Refused("belongs to Reclaim itself")

    rel = p[len(home) + 1:]
    if any(part.lower().endswith(LIBRARY_BUNDLES) for part in rel.split(os.sep)):
        raise Refused("inside a Photos/Music/TV library")
    top = rel.split(os.sep, 1)[0]
    if rules.is_protected(rel) and not (personal and top in PERSONAL_DIRS
                                        and os.sep in rel):
        raise Refused("inside a protected folder")

    # Refuse to delete an entire top-level dotfile directory like ~/.ssh.
    if os.sep not in rel and rel.startswith("."):
        raise Refused("top-level dotfile directory")
    return p


APP_DIRS = ("/Applications", os.path.join(paths.HOME, "Applications"))


def guard_app(path):
    """Raise Refused unless this is a removable, non-Apple app bundle."""
    import plistlib
    p = os.path.abspath(path)
    if os.path.islink(p) or not os.path.isdir(p):
        raise Refused("not an app bundle")
    if not p.endswith(".app"):
        raise Refused("not an .app")
    if not any(p.startswith(d + os.sep) for d in APP_DIRS):
        raise Refused("not in an Applications folder")
    if p.count(os.sep) - min(d.count(os.sep) for d in APP_DIRS if p.startswith(d + os.sep)) > 2:
        raise Refused("nested too deep inside Applications")
    try:
        with open(os.path.join(p, "Contents", "Info.plist"), "rb") as f:
            bid = plistlib.load(f).get("CFBundleIdentifier", "")
    except (OSError, ValueError, plistlib.InvalidFileException):
        raise Refused("no readable Info.plist")
    if not bid or bid.startswith("com.apple."):
        raise Refused("Apple or unidentified app")
    if bid == "com.reclaim.app":
        raise Refused("that is Reclaim itself")
    return p


def _trash_path(src):
    stamp = time.strftime(_STAMP)
    base = os.path.basename(src.rstrip(os.sep)) or "item"
    dest = os.path.join(paths.TRASH_DIR, "%s-%s" % (stamp, base))
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(paths.TRASH_DIR, "%s-%s-%d" % (stamp, base, n))
        n += 1
    return dest


def rebuildable(item):
    """Rule-verified caches: they rebuild themselves and AI never vouched."""
    return (item.get("safety") == rules.SAFE
            and item.get("source") in (None, "", "rules"))


def should_trash(item, cfg, free_bytes, policy=None):
    """Does this item keep an undo copy, or get deleted outright?"""
    if not cfg.get("undo_days", 7):
        return False
    policy = policy or cfg.get("trash_policy", "auto")
    if policy == "never":
        return False
    if policy != "auto":
        return True
    # Trash on a full disk frees nothing, so only drop the undo for things
    # that rebuild themselves, and only once space is actually tight.
    low = free_bytes / 1e9 <= cfg.get("alert_below_gb", 25)
    return not (low and rebuildable(item))


def remove(con, path, size=None, label=None, mode="manual", undo_days=7,
           trash=True, check=None):
    """Trash (or delete) one path. Returns (ok, message).

    `check` replaces the default guard, e.g. guard_app or a personal guard.
    """
    try:
        p = (check or guard)(path)
    except Refused as e:
        store.log_action(con, path, 0, label, mode, "failed", error=str(e))
        return False, str(e)

    if size is None:
        from .scan import dir_size
        size = dir_size(p)[0] if os.path.isdir(p) else os.path.getsize(p)

    paths.ensure_dirs()
    try:
        if trash and undo_days and undo_days > 0:
            dest = _trash_path(p)
            shutil.move(p, dest)
            store.log_action(con, p, size, label, mode, "trashed", trash_path=dest)
            return True, "moved to Reclaim trash"
        if os.path.isdir(p):
            shutil.rmtree(p)
        else:
            os.remove(p)
        store.log_action(con, p, size, label, mode, "deleted")
        return True, "deleted"
    except OSError as e:
        store.log_action(con, p, size, label, mode, "failed", error=str(e))
        return False, str(e)


def remove_many(con, items, mode="manual", undo_days=7, trash=True, check=None):
    """items: iterable of dicts with path/bytes/label.

    `trash` is a bool or a function item -> bool. freed_bytes counts only what
    was really deleted; trashed_bytes is still on disk until purged.
    """
    freed = trashed = 0
    ok, failed = [], []
    for it in items:
        keep = trash(it) if callable(trash) else trash
        good, msg = remove(con, it["path"], it.get("bytes"), it.get("label"),
                           mode, undo_days, trash=keep, check=check)
        if good:
            n = it.get("bytes") or 0
            if msg == "deleted":
                freed += n
            else:
                trashed += n
            ok.append(it["path"])
        else:
            failed.append({"path": it["path"], "error": msg})
    return {"freed_bytes": freed, "trashed_bytes": trashed,
            "removed": ok, "failed": failed}


def auto_cleanable(targets):
    """The subset auto-clean is allowed to touch: rule-verified safe only."""
    out = []
    for t in targets:
        if not rebuildable(t):
            continue
        try:
            guard(t["path"])
        except Refused:
            continue
        out.append(t)
    return out


def trashed_at(name, fallback):
    """When an item entered the trash, from the stamp in its name.

    Not mtime: a move keeps the folder's own mtime, so a node_modules untouched
    for months would look expired the moment it was trashed.
    """
    try:
        return time.mktime(time.strptime(name[:15], _STAMP))
    except ValueError:
        return fallback


def purge_trash(con, older_than_days=7):
    """Empty Reclaim's trash of anything past its undo window (0 = all)."""
    now = time.time()
    cutoff = now - older_than_days * 86400
    freed = 0
    if not os.path.isdir(paths.TRASH_DIR):
        return 0
    for name in os.listdir(paths.TRASH_DIR):
        p = os.path.join(paths.TRASH_DIR, name)
        try:
            if older_than_days > 0 and trashed_at(name, now) > cutoff:
                continue
            from .scan import dir_size
            size = dir_size(p)[0] if os.path.isdir(p) else os.path.getsize(p)
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
            freed += size
        except OSError:
            continue
    return freed


PURGING_PREFIX = "purging-"


def detach_trash():
    """Swap the trash for an empty one in a single rename.

    The trash looks empty at once, nothing in it can be restored half-deleted,
    and the slow part (unlinking thousands of files) can happen elsewhere.
    Returns the detached folder, or None if there was nothing to empty.
    """
    if not os.path.isdir(paths.TRASH_DIR) or not os.listdir(paths.TRASH_DIR):
        return None
    base = os.path.join(paths.DATA_DIR, PURGING_PREFIX + time.strftime(_STAMP))
    dest, n = base, 1
    while os.path.exists(dest):
        dest, n = "%s-%d" % (base, n), n + 1
    os.rename(paths.TRASH_DIR, dest)
    os.makedirs(paths.TRASH_DIR, exist_ok=True)
    return dest


def pending_purges():
    """Detached trash folders a previous run did not finish deleting."""
    if not os.path.isdir(paths.DATA_DIR):
        return []
    return sorted(os.path.join(paths.DATA_DIR, n) for n in os.listdir(paths.DATA_DIR)
                  if n.startswith(PURGING_PREFIX))


TASKPOLICY = "/usr/sbin/taskpolicy"


def _rm_command(folder):
    cmd = ["/bin/rm" if os.path.exists("/bin/rm") else "rm", "-rf", "--", folder]
    if os.path.exists(TASKPOLICY):
        # -b: background QoS for this child only, so its disk thrashing is
        # throttled without dragging the menu bar down with it.
        cmd = [TASKPOLICY, "-b"] + cmd
    return cmd


def delete_detached(folder, on_item=None, poll=1.0):
    """Delete a folder made by detach_trash in a separate rm process.

    A child process keeps the work off our GIL and lets macOS throttle it on
    its own. Progress is reported per top-level item as each one disappears.
    """
    folder = os.path.abspath(folder)
    if (os.path.dirname(folder) != os.path.abspath(paths.DATA_DIR)
            or not os.path.basename(folder).startswith(PURGING_PREFIX)
            or os.path.islink(folder)):
        raise Refused("not a detached Reclaim trash folder")

    def listing():
        try:
            return set(os.listdir(folder))
        except OSError:
            return set()

    seen = listing()
    proc = subprocess.Popen(_rm_command(folder), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    while True:
        try:
            proc.wait(timeout=poll)
            done = True
        except subprocess.TimeoutExpired:
            done = False
        left = listing()
        if on_item:
            for name in sorted(seen - left):
                on_item(name)
        seen = left
        if done:
            break
    err = proc.stderr.read().decode(errors="replace").strip().splitlines()
    proc.stderr.close()
    if os.path.exists(folder):
        raise OSError("rm exited %s: %s" % (proc.returncode, err[-1] if err else
                                           "%d items left" % len(seen)))


def restore(con, action_id):
    """Put a trashed item back where it came from."""
    row = con.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
    if not row:
        return False, "no such action"
    if row["outcome"] != "trashed" or not row["trash_path"]:
        return False, "nothing to restore"
    if not os.path.exists(row["trash_path"]):
        return False, "already purged from trash"
    if os.path.exists(row["path"]):
        return False, "something already exists at the original path"
    try:
        os.makedirs(os.path.dirname(row["path"]), exist_ok=True)
        shutil.move(row["trash_path"], row["path"])
    except OSError as e:
        return False, str(e)
    store.log_action(con, row["path"], row["bytes"], row["label"], "manual",
                     "restored")
    return True, "restored"
