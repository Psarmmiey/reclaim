"""The background half: watch free space, scan on a schedule, act when told to."""
import os
import threading
import time

from . import BUILD, __version__, version_string
from . import classify, clean, config, format as fmt, notify, rules, scan, store

CHECK_JITTER = 0.1


class Daemon:
    def __init__(self, cfg=None, con=None):
        self.cfg = cfg or config.load()
        self.con = con or store.connect()
        self.stop_event = threading.Event()
        self.last_scan_at = 0.0
        self.last_alert_level = None
        self.scanning = False
        self.emptying = None          # {"total_bytes", "freed_bytes", "started_at"}
        self.empty_done = []          # finished runs, for the menu bar to announce
        self.clipboard = None         # set by the menu bar app when it records
        self.on_config = []           # called after settings change
        self.rev = 0                  # bumped on every change the UIs should show
        self._lock = threading.Lock()
        from . import tools
        tools.load_all()
        self.tools = tools.Runner(self)

    # ------------------------------------------------------------ state

    def touch(self):
        """Something the menu bar or dashboard shows has changed."""
        self.rev += 1

    def status(self):
        s = store.latest_scan(self.con)
        free, total = scan.disk_usage(self.cfg.get("scan_root", "~"))
        targets = store.targets_for(self.con, s["id"]) if s else []
        safe = clean.auto_cleanable(targets)
        return {
            "free_bytes": free,
            "disk_bytes": total,
            "free_gb": round(fmt.gb(free), 1),
            "level": self.pressure(free),
            "scanning": self.scanning,
            "last_scan": s,
            "target_count": len(targets),
            "safe_bytes": sum(t["bytes"] for t in safe),
            "safe_count": len(safe),
            "reclaimed_total": store.reclaimed_total(self.con),
            "trash_bytes": self.trash_bytes(),
            "emptying": dict(self.emptying) if self.emptying else None,
            "rev": self.rev,
            "pid": os.getpid(),
            "version": __version__,
            "build": BUILD,
            "trash_policy": self.cfg.get("trash_policy", "auto"),
        }

    def pressure(self, free_bytes):
        g = fmt.gb(free_bytes)
        if g <= self.cfg.get("critical_below_gb", 10):
            return "critical"
        if g <= self.cfg.get("alert_below_gb", 25):
            return "low"
        return "ok"

    # ------------------------------------------------------------ actions

    def scan_now(self, on_progress=None):
        with self._lock:
            if self.scanning:
                return None
            self.scanning = True
        try:
            scan_id, targets = scan.run_scan(self.con, self.cfg, on_progress)
            n = classify.classify(self.con, scan_id, targets, self.cfg)
            store.log_event(self.con, "info",
                            "Scanned %s: %d targets, %d classified"
                            % (self.cfg.get("scan_root"), len(targets), n))
            self.last_scan_at = time.time()
            return scan_id, targets
        finally:
            self.scanning = False
            self.touch()

    def trash_decider(self, trash=None):
        """None = follow trash_policy; 'ask' falls back to auto off-screen."""
        if trash is not None:
            return trash
        free, _ = scan.disk_usage(self.cfg.get("scan_root", "~"))
        policy = self.cfg.get("trash_policy", "auto")
        if policy == "ask":
            policy = "auto"
        return lambda it: clean.should_trash(it, self.cfg, free, policy)

    def trash_bytes(self):
        return sum(r["bytes"] or 0 for r in store.restorable(self.con))

    def empty_trash(self):
        """Empty the trash, blocking. Prefer empty_trash_async from a UI."""
        if self.empty_trash_async():
            while self.emptying:
                time.sleep(0.2)
        return self.empty_done.pop()["freed_bytes"] if self.empty_done else 0

    def empty_trash_async(self):
        """Start emptying the trash on a background thread.

        Returns False if there is nothing to empty or a run is in progress.
        Progress is on self.emptying; the result lands in self.empty_done.
        """
        with self._lock:
            if self.emptying:
                return False
            sizes = {os.path.basename(r["trash_path"]): r["bytes"] or 0
                     for r in store.restorable(self.con)}
            folder = clean.detach_trash()
            if not folder:
                return False
            self.emptying = {"total_bytes": sum(sizes.values()),
                             "freed_bytes": 0, "started_at": time.time()}
            self.touch()
        self._start_purge([folder], sizes)
        return True

    def resume_purges(self):
        """Finish deleting trash a previous run detached but never removed."""
        folders = clean.pending_purges()
        if not folders:
            return
        with self._lock:
            if self.emptying:
                return
            self.emptying = {"total_bytes": 0, "freed_bytes": 0,
                             "started_at": time.time()}
        self._start_purge(folders, {})

    def _start_purge(self, folders, sizes):
        def tick(name):
            self.emptying["freed_bytes"] += sizes.get(name, 0)

        def work():
            state = self.emptying
            try:
                for f in folders:
                    clean.delete_detached(f, on_item=tick)
                store.log_event(self.con, "info", "Emptied Reclaim trash, freed %s"
                                % fmt.human(state["freed_bytes"]))
            except Exception as e:
                store.log_event(self.con, "error", "Emptying trash failed: %s" % e)
            finally:
                self.empty_done.append(dict(state))
                self.emptying = None
                self.touch()

        threading.Thread(target=work, name="reclaim-empty-trash", daemon=True).start()

    def clean_safe(self, mode="auto", trash=None):
        empty = {"freed_bytes": 0, "trashed_bytes": 0, "removed": [], "failed": []}
        s = store.latest_scan(self.con)
        if not s:
            return empty
        targets = store.targets_for(self.con, s["id"])
        items = clean.auto_cleanable(targets)
        if not items:
            return empty
        result = clean.remove_many(self.con, items, mode=mode,
                                   undo_days=self.cfg.get("undo_days", 7),
                                   trash=self.trash_decider(trash))
        store.log_event(self.con, "info", "%s clean freed %s, trashed %s"
                        % (mode, fmt.human(result["freed_bytes"]),
                           fmt.human(result["trashed_bytes"])))
        self.touch()
        return result

    # ------------------------------------------------------------ loop

    def _quiet_now(self):
        hours = self.cfg.get("auto_clean_quiet_hours") or []
        return time.localtime().tm_hour in hours

    def tick(self):
        free, _total = scan.disk_usage(self.cfg.get("scan_root", "~"))
        level = self.pressure(free)
        scan_age = time.time() - self.last_scan_at
        due = scan_age > self.cfg.get("scan_interval_hours", 12) * 3600

        if level != "ok" and self.last_alert_level != level:
            self.alert(level, free)
        if level == "ok":
            self.last_alert_level = None

        if due or (level != "ok" and scan_age > 1800):
            self.scan_now()
            self.maybe_auto_clean(free)

        purged = clean.purge_trash(self.con, self.cfg.get("undo_days", 7))
        if purged:
            store.log_event(self.con, "info",
                            "Purged %s from Reclaim trash" % fmt.human(purged))

    def alert(self, level, free):
        self.last_alert_level = level
        if not self.cfg.get("notify", True):
            return
        s = store.latest_scan(self.con)
        targets = store.targets_for(self.con, s["id"], limit=3) if s else []
        top = targets[0]["label"] if targets else "caches"
        notify.send(
            "Disk space %s" % ("critical" if level == "critical" else "low"),
            "%s free. Biggest reclaimable item: %s."
            % (fmt.human(free), top),
            subtitle="Reclaim", urgent=(level == "critical"))
        store.log_event(self.con, "warn", "Free space %s (%s)"
                        % (fmt.human(free), level))

    def maybe_auto_clean(self, free):
        if not self.cfg.get("auto_clean", True):
            return
        if fmt.gb(free) > self.cfg.get("auto_clean_below_gb", 15):
            return
        if self._quiet_now():
            return
        s = store.latest_scan(self.con)
        if not s:
            return
        items = clean.auto_cleanable(store.targets_for(self.con, s["id"]))
        total = sum(t["bytes"] for t in items)
        if fmt.gb(total) < self.cfg.get("auto_clean_min_reclaim_gb", 1.0):
            return
        result = self.clean_safe(mode="auto")
        if (result["freed_bytes"] or result["trashed_bytes"]) and self.cfg.get("notify", True):
            notify.send("Reclaim freed %s" % fmt.human(result["freed_bytes"]),
                        "Cleared %d caches that rebuild themselves."
                        % len(result["removed"]),
                        subtitle="Automatic cleanup")

    def run_forever(self):
        store.log_event(self.con, "info", "Reclaim %s started" % version_string())
        self.resume_purges()
        interval = max(60, int(self.cfg.get("check_interval_minutes", 30)) * 60)
        if not store.latest_scan(self.con):
            try:
                self.scan_now()
            except Exception as e:
                store.log_event(self.con, "error", "First scan failed: %s" % e)
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as e:
                store.log_event(self.con, "error", "Tick failed: %s" % e)
            self.stop_event.wait(interval)

    def stop(self):
        self.stop_event.set()
