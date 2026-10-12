"""Running apps, as the Force Quit window (⌘⌥Esc) lists them.

"Regular" apps only: the ones with a Dock icon. Memory and CPU include each
app's helper processes (a browser's renderers, for example), measured from
one `ps` snapshot.
"""
import os
import subprocess
import sys

NEVER = ("com.reclaim.app",)
RELAUNCHES = ("com.apple.finder",)       # macOS starts these again by itself


def ps_snapshot(runner=subprocess.run):
    """{pid: (ppid, rss_bytes, cpu_percent)} for every process we can see."""
    try:
        r = runner(["ps", "-axo", "pid=,ppid=,rss=,%cpu="], capture_output=True,
                   text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out = {}
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        try:
            out[int(parts[0])] = (int(parts[1]), int(parts[2]) * 1024, float(parts[3]))
        except ValueError:
            continue
    return out


def tree_usage(pid, snap):
    """Memory and CPU of a process plus all its descendants."""
    children = {}
    for p, (ppid, _rss, _cpu) in snap.items():
        children.setdefault(ppid, []).append(p)
    mem = cpu = 0
    stack, seen = [pid], set()
    while stack:
        p = stack.pop()
        if p in seen or p not in snap:
            continue
        seen.add(p)
        _ppid, rss, c = snap[p]
        mem += rss
        cpu += c
        stack.extend(children.get(p, ()))
    return mem, cpu


class AppKitApps:
    """The real list, from NSWorkspace."""

    def __init__(self):
        from AppKit import NSWorkspace, NSApplicationActivationPolicyRegular
        self.ws = NSWorkspace.sharedWorkspace()
        self.regular = NSApplicationActivationPolicyRegular

    def list(self):
        out = []
        for a in self.ws.runningApplications():
            if a.activationPolicy() != self.regular or a.isTerminated():
                continue
            url = a.bundleURL()
            out.append({"pid": int(a.processIdentifier()),
                        "name": str(a.localizedName() or "?"),
                        "bundle_id": str(a.bundleIdentifier() or ""),
                        "path": str(url.path()) if url else "",
                        "hidden": bool(a.isHidden()),
                        "active": bool(a.isActive())})
        return out

    def _app(self, pid):
        from AppKit import NSRunningApplication
        return NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)

    def quit(self, pid, force=False):
        a = self._app(pid)
        if a is None:
            return True                          # already gone
        return bool(a.forceTerminate() if force else a.terminate())

    def show(self, pid):
        from AppKit import NSApplicationActivateIgnoringOtherApps
        a = self._app(pid)
        return bool(a and a.activateWithOptions_(NSApplicationActivateIgnoringOtherApps))


def backend():
    if sys.platform != "darwin":
        return None
    try:
        return AppKitApps()
    except Exception:
        return None


def snapshot(apps=None, runner=subprocess.run):
    """Running apps with memory and CPU, biggest first. [] off macOS."""
    apps = apps or backend()
    if apps is None:
        return []
    snap = ps_snapshot(runner)
    rows = []
    for a in apps.list():
        mem, cpu = tree_usage(a["pid"], snap)
        rows.append(dict(a, memory=mem, cpu=round(cpu, 1),
                         protected=a["bundle_id"] in NEVER or a["pid"] == os.getpid(),
                         relaunches=a["bundle_id"] in RELAUNCHES))
    rows.sort(key=lambda r: -r["memory"])
    return rows


_OBSERVER = []


def observe_launches(callback):
    """Call `callback` whenever an app opens or quits (macOS only)."""
    if _OBSERVER:
        return _OBSERVER[0]
    from AppKit import NSWorkspace
    from Foundation import NSObject

    class _ReclaimLaunchObserver(NSObject):
        def changed_(self, _note):
            callback()

    obs = _ReclaimLaunchObserver.alloc().init()
    nc = NSWorkspace.sharedWorkspace().notificationCenter()
    for name in ("NSWorkspaceDidLaunchApplicationNotification",
                 "NSWorkspaceDidTerminateApplicationNotification"):
        nc.addObserver_selector_name_object_(obs, "changed:", name, None)
    _OBSERVER.append(obs)          # keep it alive
    return obs
