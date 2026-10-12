"""Tools: focused scans that find one kind of clutter and act on it.

Every tool returns a list of items (optionally nested one level). Acting
only ever touches items from the tool's own last scan, looked up by id, so
the dashboard can never ask Reclaim to delete an arbitrary path.

Item shape:
    id, label, bytes (int or None), detail, safety ("safe"|"review"),
    selected (default tick), kind ("path"|"app"|"personal"|"command"|"agent"),
    path (for path-like kinds), children (list of items, optional),
    note (why it is or is not selectable), disabled (cannot be acted on)
"""
import os
import subprocess
import threading
import time
import traceback

from .. import clean, format as fmt, paths, store

REGISTRY = []


def register(cls):
    REGISTRY.append(cls())
    return cls


def get(tool_id):
    for t in REGISTRY:
        if t.id == tool_id:
            return t
    return None


def flatten(items):
    for it in items:
        yield it
        for c in it.get("children") or ():
            yield c


def total(items, selected_only=False):
    n = 0
    for it in items:
        kids = it.get("children")
        if kids:
            n += total(kids, selected_only)
        elif not selected_only or it.get("selected"):
            n += it.get("bytes") or 0
    return n


class Tool:
    id = ""
    name = ""
    blurb = ""
    group = "Cleanup"
    verb = "Remove"            # button label for acting on selected items
    icon = "broom"             # dashboard picks an inline glyph by name
    unit = "item"              # what the card counts
    alt_verb = None            # a second action, e.g. Force Quit
    measure = "size"           # what `bytes` means: disk size or memory
    live = False               # rescan every time the review opens
    count_groups = False       # count top-level groups (apps) instead of leaves

    def scan(self, ctx, progress):
        """Return a list of items. `progress(msg)` updates the card."""
        raise NotImplementedError

    def act(self, ctx, items, progress):
        """Act on the selected leaf items. Default: remove path-like kinds."""
        return remove_items(ctx, items, progress)


def remove_items(ctx, items, progress):
    """Remove path items with the right guard and trash rule for each kind."""
    cfg, con = ctx.cfg, ctx.con
    days = cfg.get("undo_days", 7)
    decide = ctx.daemon.trash_decider() if ctx.daemon else True
    freed = trashed = 0
    done, failed = [], []
    for i, it in enumerate(items):
        progress("%d of %d: %s" % (i + 1, len(items), it.get("label") or it["path"]))
        kind = it.get("kind", "path")
        if kind == "app":
            check, keep = clean.guard_app, True
        elif kind == "personal":
            check, keep = (lambda p: clean.guard(p, personal=True)), True
        else:
            check = None
            keep = decide(it) if callable(decide) else decide
        if os.path.dirname(it["path"]) == os.path.join(paths.HOME, "Library", "LaunchAgents"):
            # Unload first, or the agent keeps running until the next restart.
            try:
                subprocess.run(["launchctl", "bootout", "gui/%d" % os.getuid(), it["path"]],
                               capture_output=True, timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                pass
        ok, msg = clean.remove(con, it["path"], it.get("bytes"), it.get("label"),
                               "manual", days, trash=keep, check=check)
        if ok:
            done.append(it["id"])
            if msg == "deleted":
                freed += it.get("bytes") or 0
            else:
                trashed += it.get("bytes") or 0
        else:
            failed.append({"id": it["id"], "label": it.get("label"), "error": msg})
    return {"freed_bytes": freed, "trashed_bytes": trashed,
            "done": done, "failed": failed}


class Context:
    def __init__(self, daemon):
        self.daemon = daemon
        self.cfg = daemon.cfg
        self.con = daemon.con


class Runner:
    """Runs tool scans and actions in the background, one job per tool."""

    def __init__(self, daemon):
        self.daemon = daemon
        self.state = {}
        self._lock = threading.Lock()

    def _st(self, tid):
        return self.state.setdefault(tid, {"state": "idle", "items": [],
                                           "message": "", "result": None,
                                           "scanned_at": None, "error": None})

    def summary(self):
        out = []
        for t in REGISTRY:
            st = self._st(t.id)
            items = st["items"]
            out.append({
                "id": t.id, "name": t.name, "blurb": t.blurb, "group": t.group,
                "verb": t.verb, "alt_verb": t.alt_verb, "measure": t.measure,
                "live": t.live, "icon": t.icon, "state": st["state"],
                "message": st["message"], "error": st["error"],
                "scanned_at": st["scanned_at"], "result": st["result"],
                "unit": t.unit,
                "count": (len(items) if t.count_groups else
                          sum(1 for _ in flatten(items) if not _.get("children"))),
                "bytes": total(items),
                "selected_bytes": total(items, True),
            })
        return out

    def detail(self, tid):
        t = get(tid)
        if not t:
            return None
        st = self._st(tid)
        return dict(next(s for s in self.summary() if s["id"] == tid),
                    items=st["items"])

    def _start(self, tid, state, fn):
        t = get(tid)
        if not t:
            return False
        with self._lock:
            st = self._st(tid)
            if st["state"] in ("scanning", "acting"):
                return False
            st.update(state=state, message="Starting...", error=None)
        self.daemon.touch()

        def progress(msg):
            st["message"] = msg

        def work():
            try:
                fn(t, st, progress)
            except Exception as e:
                st["error"] = str(e) or e.__class__.__name__
                store.log_event(self.daemon.con, "error", "%s failed: %s\n%s"
                                % (t.name, e, traceback.format_exc(limit=3)))
            finally:
                st["state"] = "idle"
                st["message"] = ""
                self.daemon.touch()

        threading.Thread(target=work, name="tool-" + tid, daemon=True).start()
        return True

    def scan(self, tid):
        def run(t, st, progress):
            st["items"] = t.scan(Context(self.daemon), progress)
            st["scanned_at"] = time.time()
        return self._start(tid, "scanning", run)

    def act(self, tid, ids, mode=None):
        wanted = set(ids or ())

        def run(t, st, progress):
            leaves = [it for it in flatten(st["items"])
                      if it["id"] in wanted and not it.get("children")
                      and not it.get("disabled")]
            if not leaves:
                st["result"] = {"freed_bytes": 0, "trashed_bytes": 0,
                                "done": [], "failed": []}
                return
            ctx = Context(self.daemon)
            ctx.mode = mode
            res = t.act(ctx, leaves, progress)
            st["result"] = res
            store.log_event(self.daemon.con, "info", "%s: %s on %d items, freed %s"
                            % (t.name, t.verb.lower(), len(res.get("done", [])),
                               fmt.human(res.get("freed_bytes", 0))))
            progress("Rescanning...")
            st["items"] = t.scan(Context(self.daemon), progress)
            st["scanned_at"] = time.time()
        return self._start(tid, "acting", run)


def load_all():
    """Import every tool module so each registers itself."""
    from . import apps, files, devjunk, ai, background, trashbins, runningapps  # noqa: F401
