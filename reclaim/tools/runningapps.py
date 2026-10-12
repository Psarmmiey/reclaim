"""Running Apps: the Force Quit window, with memory and CPU."""
import time

from .. import format as fmt, running
from . import Tool, register

# Tests swap in a fake; on a Mac this is the NSWorkspace list.
BACKEND = None
SETTLE = 1.5                # seconds for quitting apps to close before the rescan


def _apps():
    return BACKEND or running.backend()


@register
class RunningApps(Tool):
    id = "running"
    name = "Running Apps"
    blurb = "Everything open, like the Force Quit window, with how much memory and CPU each uses."
    group = "Performance"
    verb = "Quit"
    alt_verb = "Force Quit"
    icon = "gauge"
    unit = "app"
    measure = "memory"
    live = True                 # rescan whenever it is opened; the list moves

    def scan(self, ctx, progress):
        progress("Listing open apps...")
        apps = _apps()
        if apps is None:
            return [{"id": "none", "label": "Only available on macOS", "disabled": True,
                     "bytes": None}]
        items = []
        for a in running.snapshot(apps):
            notes = []
            if a["protected"]:
                notes.append("That's Reclaim")
            elif a["relaunches"]:
                notes.append("macOS reopens it straight away")
            if a["hidden"]:
                notes.append("hidden")
            items.append({"id": "pid:%d" % a["pid"], "pid": a["pid"], "label": a["name"],
                          "detail": "CPU %s%% · %s memory%s" % (
                              a["cpu"], fmt.human(a["memory"]),
                              " · in front" if a["active"] else ""),
                          "note": " · ".join(notes), "bytes": a["memory"],
                          "kind": "process", "selected": False,
                          "disabled": a["protected"]})
        return [{"id": "open", "label": "Open apps", "children": items}] if items else []

    def act(self, ctx, items, progress):
        apps = _apps()
        force = getattr(ctx, "mode", None) == "force"
        res = {"freed_bytes": 0, "trashed_bytes": 0, "done": [], "failed": []}
        for it in items:
            progress("%s %s" % ("Force quitting" if force else "Quitting", it["label"]))
            try:
                ok = apps.quit(it["pid"], force=force)
            except Exception as e:
                ok, err = False, str(e)
            else:
                err = "it refused; try Force Quit" if not force else "macOS refused"
            if ok:
                res["done"].append(it["id"])
            else:
                res["failed"].append({"id": it["id"], "label": it["label"], "error": err})
        time.sleep(SETTLE)
        return res
