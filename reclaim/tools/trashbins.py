"""Trash Bins: the macOS Bin, bins on other drives, and Reclaim's trash."""
import os
import subprocess

from .. import paths
from ..scan import dir_size
from . import Tool, register


def _measure(p):
    try:
        os.listdir(p)
    except PermissionError:
        return None, "Give Reclaim Full Disk Access to measure this"
    except OSError:
        return 0, ""
    return dir_size(p)[0], ""


@register
class TrashBins(Tool):
    id = "trash"
    name = "Trash Bins"
    blurb = "Empty the Bin on every drive, and Reclaim's own trash."
    group = "Cleanup"
    verb = "Empty"
    icon = "bin"

    def scan(self, ctx, progress):
        progress("Measuring the Bin...")
        out = []
        size, note = _measure(os.path.join(paths.HOME, ".Trash"))
        bins = [("Bin on this Mac", size, note)]
        uid = os.getuid()
        try:
            vols = os.listdir("/Volumes")
        except OSError:
            vols = []
        for v in vols:
            p = os.path.join("/Volumes", v, ".Trashes", str(uid))
            if os.path.isdir(p):
                s, n = _measure(p)
                bins.append(("Bin on %s" % v, s, n))
        if any(b[1] is None or b[1] for b in bins):
            out.append({"id": "trash:finder", "label": "Bin", "kind": "finder-bin",
                        "bytes": sum(b[1] or 0 for b in bins), "selected": True,
                        "detail": ", ".join(b[0] for b in bins),
                        "note": next((b[2] for b in bins if b[2]), "")})
        rb = ctx.daemon.trash_bytes() if ctx.daemon else 0
        if rb:
            out.append({"id": "trash:reclaim", "label": "Reclaim trash", "kind": "reclaim-bin",
                        "bytes": rb, "selected": True,
                        "detail": "Things Reclaim removed, kept for undo"})
        return out

    def act(self, ctx, items, progress):
        res = {"freed_bytes": 0, "trashed_bytes": 0, "done": [], "failed": []}
        for it in items:
            if it["kind"] == "finder-bin":
                progress("Asking Finder to empty the Bin...")
                r = subprocess.run(["osascript", "-e",
                                    'tell application "Finder" to empty trash'],
                                   capture_output=True, text=True, timeout=1800)
                if r.returncode == 0:
                    res["done"].append(it["id"])
                    res["freed_bytes"] += it.get("bytes") or 0
                else:
                    res["failed"].append({"id": it["id"], "label": it["label"],
                                          "error": r.stderr.strip() or "Finder refused"})
            elif it["kind"] == "reclaim-bin":
                if ctx.daemon.empty_trash_async():
                    res["done"].append(it["id"])
                    res["freed_bytes"] += it.get("bytes") or 0
        return res
