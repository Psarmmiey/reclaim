"""Walk the disk and turn it into a list of things worth acting on."""
import fnmatch
import os
import shutil
import stat
import time

from . import paths, rules

SKIP_NAMES = {".Spotlight-V100", ".fseventsd", ".DocumentRevisions-V100",
              ".TemporaryItems", "System Volume Information", "$RECYCLE.BIN"}

# Once we identify a folder as a target we record its size and stop descending,
# so a node_modules inside a node_modules is counted once.
STOP_AT = {name for name, _, _ in rules.DIR_PATTERNS}


def disk_usage(path="/"):
    """Free and total bytes for the volume that actually holds user data."""
    for candidate in ("/System/Volumes/Data", path, "/"):
        try:
            u = shutil.disk_usage(candidate)
            return u.free, u.total
        except OSError:
            continue
    return 0, 0


def dir_size(path, follow=False):
    """Bytes and file count under a directory, hard links counted once."""
    total = 0
    count = 0
    seen = set()
    stack = [path]
    while stack:
        p = stack.pop()
        try:
            entries = list(os.scandir(p))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                    continue
                st = e.stat(follow_symlinks=False)
                if not stat.S_ISREG(st.st_mode):
                    continue
                if getattr(st, "st_nlink", 1) > 1:
                    key = (st.st_dev, st.st_ino)
                    if key in seen:
                        continue
                    seen.add(key)
                total += st.st_size
                count += 1
            except OSError:
                continue
    return total, count


class Scanner:
    def __init__(self, root, min_bytes=200 * 1024 * 1024, exclude=(),
                 on_progress=None):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.min_bytes = min_bytes
        self.exclude = list(exclude)
        self.on_progress = on_progress
        self.total_bytes = 0
        self.file_count = 0
        self.dir_count = 0
        self.errors = 0
        self.targets = []
        self._seen_inodes = set()
        self._last_tick = 0.0

    # -------------------------------------------------------------- helpers

    def rel(self, path):
        if path.startswith(self.root):
            return path[len(self.root):].lstrip(os.sep)
        return path

    def excluded(self, name, path):
        if name in SKIP_NAMES:
            return True
        # Reclaim's own trash and state are never scan material.
        if path == paths.DATA_DIR or path.startswith(paths.DATA_DIR + os.sep):
            return True
        if path == paths.CONFIG_DIR or path.startswith(paths.CONFIG_DIR + os.sep):
            return True
        for pat in self.exclude:
            if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(path, pat):
                return True
        return False

    def tick(self):
        if not self.on_progress:
            return
        now = time.time()
        if now - self._last_tick < 0.4:
            return
        self._last_tick = now
        self.on_progress(self.dir_count, self.file_count, self.total_bytes)

    def add_target(self, path, size, kind, label, safety, mtime=None,
                   reason=None):
        if size < self.min_bytes:
            return
        if rules.is_protected(self.rel(path)):
            safety = rules.KEEP
            reason = reason or "inside a protected folder"
        self.targets.append({
            "path": path, "bytes": size, "kind": kind, "label": label,
            "safety": safety, "mtime": mtime, "reason": reason,
            "source": "rules",
        })

    # ---------------------------------------------------------------- walk

    def run(self):
        started = time.time()
        self._walk(self.root, 0)
        self._known_caches()
        self._dedupe()
        self.targets.sort(key=lambda t: -t["bytes"])
        self.elapsed = time.time() - started
        return self.targets

    def _walk(self, root, depth):
        stack = [(root, depth)]
        while stack:
            path, d = stack.pop()
            self.dir_count += 1
            self.tick()
            try:
                entries = list(os.scandir(path))
            except OSError:
                self.errors += 1
                continue

            for e in entries:
                try:
                    if e.is_symlink():
                        continue
                    if self.excluded(e.name, e.path):
                        continue

                    if e.is_dir(follow_symlinks=False):
                        hit = self._match_dir(e.name)
                        if hit and d > 0:
                            label, safety = hit
                            size, files = dir_size(e.path)
                            self.total_bytes += size
                            self.file_count += files
                            st = self._safe_stat(e.path)
                            self.add_target(e.path, size, "dir", label, safety,
                                            st.st_mtime if st else None)
                            continue          # do not descend into a target
                        if d < 24:
                            stack.append((e.path, d + 1))
                        continue

                    st = e.stat(follow_symlinks=False)
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    if getattr(st, "st_nlink", 1) > 1:
                        key = (st.st_dev, st.st_ino)
                        if key in self._seen_inodes:
                            continue
                        self._seen_inodes.add(key)
                    self.total_bytes += st.st_size
                    self.file_count += 1
                    self._match_file(e.name, e.path, st)
                except OSError:
                    self.errors += 1

    def _safe_stat(self, path):
        try:
            return os.stat(path)
        except OSError:
            return None

    @staticmethod
    def _match_dir(name):
        for pat, label, safety in rules.DIR_PATTERNS:
            if name == pat:
                return label, safety
        return None

    def _match_file(self, name, path, st):
        if st.st_size < self.min_bytes:
            return
        for pat, label, safety in rules.FILE_PATTERNS:
            if fnmatch.fnmatch(name, pat):
                self.add_target(path, st.st_size, "file", label, safety,
                                st.st_mtime)
                return
        # Anything genuinely huge is worth surfacing even without a rule.
        if st.st_size >= max(self.min_bytes * 2, 512 * 1024 ** 2):
            age_days = (time.time() - st.st_mtime) / 86400
            self.add_target(
                path, st.st_size, "file", "large file", rules.REVIEW,
                st.st_mtime,
                "untouched for %d days" % age_days if age_days > 180 else None)

    def _known_caches(self):
        """Explicit cache locations, checked whether or not the walk saw them."""
        home = os.path.expanduser("~")
        for rel, label, safety in rules.HOME_CACHES:
            p = os.path.join(home, rel)
            if not os.path.isdir(p):
                continue
            if any(t["path"] == p for t in self.targets):
                continue
            size, _files = dir_size(p)
            st = self._safe_stat(p)
            self.add_target(p, size, "cache", label, safety,
                            st.st_mtime if st else None)

    def _dedupe(self):
        """Drop any target that lives inside another target."""
        kept = []
        for t in sorted(self.targets, key=lambda t: len(t["path"])):
            parent = next((k for k in kept
                           if t["path"].startswith(k["path"] + os.sep)), None)
            if parent is None:
                kept.append(t)
        self.targets = kept


def run_scan(con, cfg, on_progress=None):
    """Scan, persist, and return (scan_id, targets)."""
    from . import store
    root = cfg.get("scan_root") or os.path.expanduser("~")
    scan_id = store.start_scan(con, root)
    sc = Scanner(root,
                 min_bytes=int(cfg.get("min_target_mb", 200)) * 1024 * 1024,
                 exclude=cfg.get("exclude", []),
                 on_progress=on_progress)
    targets = sc.run()
    free, total = disk_usage(root)
    store.add_targets(con, scan_id, targets)
    store.finish_scan(con, scan_id,
                      total_bytes=sc.total_bytes, file_count=sc.file_count,
                      dir_count=sc.dir_count, free_bytes=free,
                      disk_bytes=total, errors=sc.errors)
    return scan_id, targets
