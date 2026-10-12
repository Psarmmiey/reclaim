"""Large and Old Files, Duplicate Finder, Downloads."""
import hashlib
import os
import stat
import time

from .. import paths
from . import Tool, register

DAY = 86400
SKIP_DIRS = {"Library", "node_modules", ".git", ".Trash", "Applications",
             ".venv", "venv", "__pycache__", "build", "dist", ".gradle", ".m2",
             ".npm", ".cache", "Pods", "DerivedData", "vendor"}
SKIP_EXT = (".app", ".photoslibrary", ".musiclibrary", ".tvlibrary",
            ".imovielibrary", ".fcpbundle", ".bundle", ".framework", ".xcarchive")
DATALESS = getattr(stat, "SF_DATALESS", 0x40000000)   # iCloud, not downloaded


def walk_files(root, min_bytes, progress=None, skip_hidden=True):
    """Yield (path, stat) for regular, local files of at least min_bytes."""
    stack, seen = [root], 0
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            name = e.name
            if skip_hidden and name.startswith("."):
                continue
            try:
                if e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    if name in SKIP_DIRS or name.lower().endswith(SKIP_EXT):
                        continue
                    stack.append(e.path)
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            seen += 1
            if progress and seen % 5000 == 0:
                progress("Looked at %s files..." % format(seen, ","))
            if not stat.S_ISREG(st.st_mode) or st.st_size < min_bytes:
                continue
            if getattr(st, "st_flags", 0) & DATALESS:
                continue
            yield e.path, st


def _age(ts):
    days = max(0, int((time.time() - ts) / DAY))
    if days < 1:
        return "today"
    if days < 60:
        return "%d days ago" % days
    if days < 730:
        return "%d months ago" % (days // 30)
    return "%d years ago" % (days // 365)


def _file_item(p, st, selected=False, detail=None):
    used = max(st.st_mtime, st.st_atime)
    return {"id": p, "path": p, "label": os.path.basename(p),
            "detail": detail or "%s · last used %s" % (
                os.path.dirname(p).replace(paths.HOME, "~"), _age(used)),
            "bytes": st.st_size, "kind": "personal", "safety": "review",
            "selected": selected, "used_at": used}


@register
class LargeOld(Tool):
    id = "large"
    name = "Large and Old Files"
    blurb = "Big files in your home folder, oldest first. Nothing is ticked for you."
    group = "My Clutter"
    icon = "box"

    MIN = 100 * 1024 * 1024
    OLD_DAYS = 180

    def scan(self, ctx, progress):
        now = time.time()
        big, old = [], []
        for p, st in walk_files(paths.HOME, self.MIN, progress):
            it = _file_item(p, st)
            (old if now - it["used_at"] > self.OLD_DAYS * DAY else big).append(it)
        old.sort(key=lambda it: -it["bytes"])
        big.sort(key=lambda it: -it["bytes"])
        out = []
        if old:
            out.append({"id": "old", "label": "Not used for 6 months", "children": old[:300]})
        if big:
            out.append({"id": "big", "label": "Large, used recently", "children": big[:300]})
        return out


def _hash(path, limit=None):
    h = hashlib.blake2b(digest_size=20)
    try:
        with open(path, "rb") as f:
            if limit:
                h.update(f.read(limit))
            else:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def find_duplicates(root, min_bytes, progress=None):
    """Groups of byte-identical files: size, then first 64 KB, then full hash."""
    by_size = {}
    for p, st in walk_files(root, min_bytes, progress):
        by_size.setdefault(st.st_size, []).append((p, st))
    groups = []
    cands = [g for g in by_size.values() if len(g) > 1]
    for i, same in enumerate(cands):
        if progress and i % 50 == 0:
            progress("Comparing %d of %d size groups..." % (i, len(cands)))
        inodes, uniq = set(), []
        for p, st in same:                       # hard links are not duplicates
            if (st.st_dev, st.st_ino) not in inodes:
                inodes.add((st.st_dev, st.st_ino))
                uniq.append((p, st))
        heads = {}
        for p, st in uniq:
            heads.setdefault(_hash(p, 65536), []).append((p, st))
        for k, hs in heads.items():
            if k is None or len(hs) < 2:
                continue
            fulls = {}
            for p, st in hs:
                fulls.setdefault(_hash(p), []).append((p, st))
            groups += [g for h, g in fulls.items() if h and len(g) > 1]
    return groups


def _keeper(group):
    """The copy to keep: not in Downloads, then oldest, then shortest path."""
    dl = os.path.join(paths.HOME, "Downloads") + os.sep
    return min(group, key=lambda ps: (ps[0].startswith(dl), ps[1].st_mtime, len(ps[0])))


@register
class Duplicates(Tool):
    id = "duplicates"
    name = "Duplicate Finder"
    blurb = "Byte-for-byte identical files. One copy of each is always kept."
    group = "My Clutter"
    icon = "copies"
    unit = "set"
    count_groups = True

    MIN = 1024 * 1024

    def scan(self, ctx, progress):
        items = []
        for g in find_duplicates(paths.HOME, self.MIN, progress):
            keep = _keeper(g)
            kids = []
            for p, st in sorted(g, key=lambda ps: ps[0]):
                it = _file_item(p, st, selected=(p != keep[0]),
                                detail=os.path.dirname(p).replace(paths.HOME, "~"))
                if p == keep[0]:
                    it.update(disabled=True, note="Kept")
                kids.append(it)
            items.append({"id": "dup:" + keep[0], "label": os.path.basename(keep[0]),
                          "detail": "%d copies" % len(g), "children": kids})
        items.sort(key=lambda it: -sum(k["bytes"] for k in it["children"] if not k.get("disabled")))
        return items[:500]

    def act(self, ctx, items, progress):
        # Re-check at act time: never remove the last copy of anything.
        from . import remove_items
        st = ctx.daemon.tools.state.get(self.id, {})
        safe = []
        for it in items:
            group = next((g for g in st.get("items", [])
                          if any(k["id"] == it["id"] for k in g["children"])), None)
            if group and any(k.get("disabled") and os.path.exists(k["path"])
                             for k in group["children"]):
                safe.append(it)
        return remove_items(ctx, safe, progress)


KINDS = [
    ("Installers", (".dmg", ".pkg", ".mpkg", ".iso", ".xip")),
    ("Archives", (".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar")),
    ("Documents", (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".ppt",
                   ".pptx", ".pages", ".numbers", ".key", ".txt", ".md", ".epub")),
    ("Images", (".png", ".jpg", ".jpeg", ".gif", ".heic", ".webp", ".svg", ".tiff")),
    ("Video and audio", (".mp4", ".mov", ".mkv", ".avi", ".mp3", ".m4a", ".wav")),
]


@register
class Downloads(Tool):
    id = "downloads"
    name = "Downloads"
    blurb = "One-off files in Downloads. Installers older than a week are ticked."
    group = "My Clutter"
    icon = "download"

    def scan(self, ctx, progress):
        root = os.path.join(paths.HOME, "Downloads")
        now = time.time()
        buckets = {name: [] for name, _ in KINDS}
        buckets["Other"] = []
        for p, st in walk_files(root, 1024 * 1024, progress, skip_hidden=True):
            ext = os.path.splitext(p)[1].lower()
            kind = next((n for n, exts in KINDS if ext in exts), "Other")
            tick = kind == "Installers" and now - st.st_mtime > 7 * DAY
            buckets[kind].append(_file_item(p, st, selected=tick))
        out = []
        for name, kids in buckets.items():
            if kids:
                kids.sort(key=lambda it: -it["bytes"])
                out.append({"id": "dl:" + name, "label": name, "children": kids})
        return out
