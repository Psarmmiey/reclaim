"""Uninstaller and App Leftovers."""
import os
import plistlib
import re
import shutil
import subprocess

from .. import clean, paths
from ..scan import dir_size
from . import Tool, register

LIB = os.path.join(paths.HOME, "Library")

# (folder under ~/Library, how names relate to a bundle id)
#   "exact":  <id> or <id>.<helper>
#   "suffix": <id><suffix>
LEFTOVER_PLACES = [
    ("Application Support", "exact", ""),
    ("Caches", "exact", ""),
    ("Containers", "exact", ""),
    ("Application Scripts", "exact", ""),
    ("HTTPStorages", "exact", ""),
    ("WebKit", "exact", ""),
    ("Logs", "exact", ""),
    ("Saved Application State", "suffix", ".savedState"),
    ("Preferences", "suffix", ".plist"),
    ("Cookies", "suffix", ".binarycookies"),
    ("LaunchAgents", "suffix", ".plist"),
]
# Folders where a plain app name (not a bundle id) also identifies leftovers.
NAME_PLACES = ("Application Support", "Caches", "Logs")
GENERIC_NAMES = {"code", "app", "data", "cache", "caches", "logs", "default",
                 "helper", "update", "updater", "google", "microsoft", "apple"}
BUNDLE_ID_RE = re.compile(r"^[A-Za-z0-9-]+\.[A-Za-z0-9-]+\.[A-Za-z0-9._-]+$")
NEVER = ("com.apple.", "com.reclaim.")


def _plist(path):
    try:
        with open(os.path.join(path, "Contents", "Info.plist"), "rb") as f:
            return plistlib.load(f)
    except Exception:
        return {}


def app_dirs():
    return [d for d in ("/Applications", os.path.join(paths.HOME, "Applications"))
            if os.path.isdir(d)]


def installed_apps():
    """Top-level apps, plus apps one folder down (Setapp, Adobe...)."""
    out = []
    for root in app_dirs():
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            if name.endswith(".app"):
                out.append(p)
            elif os.path.isdir(p) and not os.path.islink(p) and not name.startswith("."):
                try:
                    out += [os.path.join(p, n) for n in sorted(os.listdir(p))
                            if n.endswith(".app")]
                except OSError:
                    pass
    return out


def known_bundle_ids():
    """Every bundle id on the system we can find, helpers included.

    Leftover detection must err towards "still installed", so this casts a
    wide net: Spotlight first, then the Applications folders as a fallback.
    """
    ids = set()
    try:
        r = subprocess.run(["mdfind", "kMDItemContentType == com.apple.application-bundle"],
                           capture_output=True, text=True, timeout=60)
        for p in r.stdout.splitlines():
            bid = _plist(p).get("CFBundleIdentifier")
            if bid:
                ids.add(bid.lower())
    except (OSError, subprocess.TimeoutExpired):
        pass
    for p in installed_apps():
        bid = _plist(p).get("CFBundleIdentifier")
        if bid:
            ids.add(bid.lower())
    return ids


def _matches(name, bid):
    n, b = name.lower(), bid.lower()
    return n == b or n.startswith(b + ".")


def leftovers(bid, names=()):
    """Paths under ~/Library that belong to this bundle id (or app name)."""
    found = []
    names = {n.lower() for n in names if n and len(n) >= 4
             and n.lower() not in GENERIC_NAMES}
    for folder, mode, suffix in LEFTOVER_PLACES:
        base = os.path.join(LIB, folder)
        try:
            entries = os.listdir(base)
        except OSError:
            continue
        for e in entries:
            stem = e[:-len(suffix)] if suffix and e.endswith(suffix) else e
            if suffix and not e.endswith(suffix):
                continue
            if _matches(stem, bid) or (folder in NAME_PLACES and e.lower() in names):
                found.append(os.path.join(base, e))
    gc = os.path.join(LIB, "Group Containers")
    try:
        for e in os.listdir(gc):
            # TEAMID.com.vendor.app or group.com.vendor.app
            tail = e.split(".", 1)[1] if "." in e else e
            if _matches(tail, bid) or _matches(e, bid):
                found.append(os.path.join(gc, e))
    except OSError:
        pass
    byhost = os.path.join(LIB, "Preferences", "ByHost")
    try:
        found += [os.path.join(byhost, e) for e in os.listdir(byhost)
                  if e.lower().startswith(bid.lower() + ".")]
    except OSError:
        pass
    return sorted(set(found))


def _size(p):
    try:
        if os.path.isdir(p) and not os.path.islink(p):
            return dir_size(p)[0]
        return os.lstat(p).st_size
    except OSError:
        return 0


def _running(app_path):
    try:
        r = subprocess.run(["pgrep", "-f", os.path.join(app_path, "Contents", "MacOS") + "/"],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _leftover_item(p, selected=True):
    rel = os.path.relpath(p, LIB)
    where = rel.split(os.sep)[0] if not rel.startswith("..") else os.path.basename(p)
    if where == "Preferences" and os.sep in rel[len("Preferences/"):]:
        where = "Preferences (per Mac)"
    return {"id": p, "path": p, "label": where,
            "detail": p.replace(paths.HOME, "~"), "bytes": _size(p),
            "kind": "path", "safety": "review", "selected": selected}


@register
class Uninstaller(Tool):
    id = "uninstaller"
    name = "Uninstaller"
    blurb = "Remove apps completely, with the support files, caches and preferences they leave behind."
    group = "Applications"
    verb = "Uninstall"
    icon = "apps"
    unit = "app"
    count_groups = True

    def scan(self, ctx, progress):
        apps = installed_apps()
        items = []
        for i, app in enumerate(apps):
            info = _plist(app)
            bid = info.get("CFBundleIdentifier", "")
            name = info.get("CFBundleDisplayName") or info.get("CFBundleName") \
                or os.path.basename(app)[:-4]
            progress("Measuring %s (%d of %d)" % (name, i + 1, len(apps)))
            note, disabled = "", False
            try:
                clean.guard_app(app)
            except clean.Refused as e:
                note, disabled = str(e), True
            if not disabled and _running(app):
                note, disabled = "Quit it first", True
            if not disabled and not os.access(os.path.dirname(app), os.W_OK):
                note = "Needs your password"
            app_item = {"id": app, "path": app, "label": name + " app",
                        "detail": "%s · %s" % (info.get("CFBundleShortVersionString", ""), bid),
                        "bytes": _size(app), "kind": "app", "safety": "review",
                        "selected": False, "disabled": disabled, "note": note}
            kids = [app_item]
            if bid and not bid.startswith(NEVER):
                kids += [dict(_leftover_item(p, False), disabled=disabled)
                         for p in leftovers(bid, [name, os.path.basename(app)[:-4]])]
            items.append({"id": "app:" + app, "label": name,
                          "detail": app.replace(paths.HOME, "~"),
                          "children": kids, "disabled": disabled, "note": note,
                          "selected": False})
        items.sort(key=lambda it: -sum(c["bytes"] or 0 for c in it["children"]))
        return items

    def act(self, ctx, items, progress):
        res = super().act(ctx, items, progress)
        # Apps owned by root (App Store, installer packages) cannot be moved by
        # us; hand those to Finder, which asks for the password itself.
        for f in list(res["failed"]):
            it = next((x for x in items if x["id"] == f["id"]), None)
            if it and it.get("kind") == "app" and "ermission" in f["error"]:
                script = 'tell application "Finder" to delete (POSIX file %s as alias)' \
                    % _q(it["path"])
                r = subprocess.run(["osascript", "-e", script], capture_output=True,
                                   text=True, timeout=120)
                if r.returncode == 0:
                    res["failed"].remove(f)
                    res["done"].append(it["id"])
                    res["trashed_bytes"] += it.get("bytes") or 0
                    f["error"] = "moved to the Bin by Finder"
        return res


def _q(s):
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')


@register
class AppLeftovers(Tool):
    id = "leftovers"
    name = "App Leftovers"
    blurb = "Find files left behind by apps you have already deleted."
    group = "Applications"
    icon = "ghost"
    unit = "removed app"
    count_groups = True

    def scan(self, ctx, progress):
        progress("Listing installed apps...")
        known = known_bundle_ids()
        vendors = {".".join(b.split(".")[:2]) for b in known}
        groups = {}
        # LaunchAgents are left to the Background tool, which unloads them properly.
        places = [x for x in LEFTOVER_PLACES if x[0] != "LaunchAgents"]
        for folder, mode, suffix in places + [("Group Containers", "exact", "")]:
            progress("Checking ~/Library/%s" % folder)
            base = os.path.join(LIB, folder)
            try:
                entries = os.listdir(base)
            except OSError:
                continue
            for e in entries:
                stem = e[:-len(suffix)] if suffix and e.endswith(suffix) else e
                if folder == "Group Containers" and "." in stem:
                    first, rest = stem.split(".", 1)
                    if first == "group" or re.fullmatch(r"[A-Z0-9]{10}", first):
                        stem = rest
                if not BUNDLE_ID_RE.match(stem):
                    continue
                low = stem.lower()
                if low.startswith(NEVER) or any(low == k or low.startswith(k + ".")
                                                or k.startswith(low + ".") for k in known):
                    continue
                key = ".".join(low.split(".")[:3])
                groups.setdefault(key, []).append(os.path.join(base, e))
        items = []
        for key, ps in groups.items():
            vendor = ".".join(key.split(".")[:2])
            # Same vendor still installed: could be a helper we did not spot.
            sure = vendor not in vendors
            kids = [_leftover_item(p, sure) for p in sorted(ps)]
            if sum(k["bytes"] for k in kids) < 4096 and not sure:
                continue
            items.append({"id": "lo:" + key, "label": key,
                          "detail": "%d item%s" % (len(kids), "" if len(kids) == 1 else "s"),
                          "note": "" if sure else "Another app from this developer is installed",
                          "children": kids, "selected": sure})
        items.sort(key=lambda it: -sum(k["bytes"] for k in it["children"]))
        return items
