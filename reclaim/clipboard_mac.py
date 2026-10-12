"""macOS half of clipboard history: AppKit pasteboard, hotkey, picker, paste.

Only imported by the menu bar app. Everything that can be tested without a
Mac lives in clipboard.py.
"""
import ctypes
import threading
import time

import objc
from AppKit import (NSApplication, NSApplicationActivateIgnoringOtherApps,
                    NSBitmapImageFileTypePNG, NSBitmapImageRep, NSEvent,
                    NSEventMaskKeyDown, NSMenu, NSPasteboard,
                    NSPasteboardTypePNG, NSPasteboardTypeString,
                    NSPasteboardTypeTIFF, NSPasteboardURLReadingFileURLsOnlyKey,
                    NSRunningApplication, NSWorkspace)
from Foundation import (NSURL, NSData, NSDictionary, NSDistributedNotificationCenter,
                        NSObject)

from . import clipboard

from .clipboard import MOD_MASK, parse_hotkey  # noqa: E402

V_KEY = 9


class AppKitPasteboard:
    def __init__(self):
        self.pb = NSPasteboard.generalPasteboard()

    def change_count(self):
        return self.pb.changeCount()

    def types(self):
        return [str(t) for t in (self.pb.types() or [])]

    def string(self):
        s = self.pb.stringForType_(NSPasteboardTypeString)
        return str(s) if s is not None else None

    def file_paths(self):
        urls = self.pb.readObjectsForClasses_options_(
            [NSURL], {NSPasteboardURLReadingFileURLsOnlyKey: True}) or []
        return [str(u.path()) for u in urls]

    def image_png(self):
        data = self.pb.dataForType_(NSPasteboardTypePNG)
        if data is None:
            tiff = self.pb.dataForType_(NSPasteboardTypeTIFF)
            if tiff is None:
                return None
            rep = NSBitmapImageRep.imageRepWithData_(tiff)
            if rep is None:
                return None
            data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
        return bytes(data) if data is not None else None

    def frontmost_app(self):
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(app.localizedName()) if app else None

    def write(self, text=None, files=None, png=None):
        self.pb.clearContents()
        if files:
            self.pb.writeObjects_([NSURL.fileURLWithPath_(p) for p in files])
        elif png:
            self.pb.setData_forType_(NSData.dataWithBytes_length_(png, len(png)),
                                     NSPasteboardTypePNG)
        else:
            self.pb.setString_forType_(text or "", NSPasteboardTypeString)


# --------------------------------------------------------------- paste

_AS = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
_CF = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_AS.AXIsProcessTrusted.restype = ctypes.c_bool
_AS.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
_AS.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
_AS.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
_AS.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
_AS.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
_AS.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_CF.CFRelease.argtypes = [ctypes.c_void_p]
K_CG_HID_EVENT_TAP = 0
K_CG_FLAG_COMMAND = 1 << 20


def can_paste(prompt=False):
    """Is Reclaim allowed to type for you (Accessibility)? Optionally ask."""
    if not prompt:
        return bool(_AS.AXIsProcessTrusted())
    opts = NSDictionary.dictionaryWithObject_forKey_(True, "AXTrustedCheckOptionPrompt")
    return bool(_AS.AXIsProcessTrustedWithOptions(objc.pyobjc_id(opts)))


def press_cmd_v():
    for down in (True, False):
        ev = _AS.CGEventCreateKeyboardEvent(None, V_KEY, down)
        if not ev:
            return False
        _AS.CGEventSetFlags(ev, K_CG_FLAG_COMMAND)
        _AS.CGEventPost(K_CG_HID_EVENT_TAP, ev)
        _CF.CFRelease(ev)
    return True


# --------------------------------------------------------------- idle

class _IdleObserver(NSObject):
    """Pauses clipboard polling while nobody can be copying anything:
    screen locked, displays asleep, or the Mac going to sleep."""

    def initWithOwner_(self, owner):
        self = objc.super(_IdleObserver, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def idle_(self, _note):
        self.owner.pause()

    def active_(self, _note):
        self.owner.resume()


def _observe_idle(owner):
    obs = _IdleObserver.alloc().initWithOwner_(owner)
    ws = NSWorkspace.sharedWorkspace().notificationCenter()
    for name in ("NSWorkspaceScreensDidSleepNotification", "NSWorkspaceWillSleepNotification"):
        ws.addObserver_selector_name_object_(obs, "idle:", name, None)
    for name in ("NSWorkspaceScreensDidWakeNotification", "NSWorkspaceDidWakeNotification"):
        ws.addObserver_selector_name_object_(obs, "active:", name, None)
    dist = NSDistributedNotificationCenter.defaultCenter()
    dist.addObserver_selector_name_object_(obs, "idle:", "com.apple.screenIsLocked", None)
    dist.addObserver_selector_name_object_(obs, "active:", "com.apple.screenIsUnlocked", None)
    return obs


# --------------------------------------------------------------- picker

class Clipboard:
    """Owns the watcher, the hotkey, and the pop-up picker."""

    def __init__(self, cfg, on_change=None, log=None):
        import rumps
        self.rumps = rumps
        self.watcher = clipboard.Watcher(AppKitPasteboard(), cfg)
        self.on_change = on_change
        self.log = log or (lambda msg: None)
        self.timer = rumps.Timer(self._tick, 0.5)
        self.monitors = []
        self.prev_app = None
        self.trusted = False
        self.ticks = 0
        self.paused = False
        self.idle_observer = None

    @property
    def cfg(self):
        return self.watcher.cfg

    def start(self):
        self.trusted = can_paste()
        self.log("Clipboard shortcut %s: Accessibility %s"
                 % (self.cfg.get("clipboard_hotkey"), "granted" if self.trusted else "not granted"))
        if not self.trusted and self.cfg.get("clipboard", True) and self._may_nag():
            # The shortcut can only be heard in other apps with Accessibility,
            # so ask now rather than on first use (which it would never reach).
            can_paste(prompt=True)
        self._start_timer()
        try:
            self.idle_observer = _observe_idle(self)
        except Exception as e:
            self.log("Clipboard idle pause unavailable: %r" % e)
        self.install_hotkey()
        if self.on_change:
            self.on_change()

    def _start_timer(self):
        self.timer.start()
        try:
            # Half a second of slack lets macOS batch this with other wake-ups.
            self.timer._nstimer.setTolerance_(0.25)
        except Exception:
            pass

    def pause(self):
        if not self.paused:
            self.paused = True
            self.timer.stop()

    def resume(self):
        if self.paused:
            self.paused = False
            self._tick(None)                       # catch anything copied meanwhile
            self._start_timer()

    @staticmethod
    def _may_nag():
        """Ask at most once a day; the menu item is always there."""
        import os
        from . import paths
        stamp = os.path.join(paths.DATA_DIR, "accessibility-asked")
        try:
            if time.time() - os.path.getmtime(stamp) < 86400:
                return False
        except OSError:
            pass
        try:
            with open(stamp, "w"):
                pass
        except OSError:
            pass
        return True

    def open_accessibility_settings(self):
        import subprocess
        can_paste(prompt=True)
        subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security"
                        "?Privacy_Accessibility"], check=False)

    # -- recording
    def _tick(self, _):
        try:
            if self.watcher.poll() and self.on_change:
                self.on_change()
            self.ticks += 1
            if self.ticks % 6 == 0:                 # every 3 seconds
                now = can_paste()
                if now != self.trusted:
                    self.trusted = now
                    self.log("Accessibility %s" % ("granted" if now else "revoked"))
                    self.install_hotkey()           # monitors made before the grant stay deaf
                    if self.on_change:
                        self.on_change()
        except Exception as e:
            self.log("Clipboard tick failed: %r" % e)

    # -- hotkey
    def install_hotkey(self):
        for m in self.monitors:
            NSEvent.removeMonitor_(m)
        self.monitors = []
        hk = parse_hotkey(self.cfg.get("clipboard_hotkey", "ctrl+cmd+v"))
        if not hk or not self.cfg.get("clipboard", True):
            return
        code, flags = hk

        def matches(ev):
            return ev.keyCode() == code and (ev.modifierFlags() & MOD_MASK) == flags

        def global_handler(ev):
            if matches(ev):
                self.show_picker()

        def local_handler(ev):
            if matches(ev):
                self.show_picker()
                return None
            return ev

        # The global monitor only sees keys once Accessibility is granted.
        self.monitors.append(NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSEventMaskKeyDown, global_handler))
        self.monitors.append(NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            NSEventMaskKeyDown, local_handler))

    def items(self, limit=20):
        return clipboard.recent(self.watcher.con, limit)

    def build_menu(self, limit=20, numbered=True):
        rumps = self.rumps
        out = []
        for i, c in enumerate(self.items(limit)):
            title = ("★ " if c["pinned"] else "") + clipboard.title(c)
            key = str(i + 1) if numbered and i < 9 else ""
            it = rumps.MenuItem(title, callback=self._chosen, key=key)
            it.clip_id = c["id"]
            out.append(it)
        if not out:
            out.append(rumps.MenuItem("Nothing copied yet"))
        return out

    def show_picker(self):
        """Pop the history up at the mouse pointer, keyboard ready."""
        try:
            self._show_picker()
        except Exception:
            import traceback
            self.log("Clipboard picker failed:\n" + traceback.format_exc(limit=6))

    def _show_picker(self):
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        me = NSRunningApplication.currentApplication()
        # Remember where you were, so the paste lands back there.
        if front is not None and front.processIdentifier() != me.processIdentifier():
            self.prev_app = front
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        for it in self.build_menu():
            item = it._menuitem
            item.setKeyEquivalentModifierMask_(0)
            menu.addItem_(item)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        menu.popUpMenuPositioningItem_atLocation_inView_(None, NSEvent.mouseLocation(), None)

    def _chosen(self, sender):
        self.choose(getattr(sender, "clip_id", None))

    def choose(self, clip_id):
        """Copy a clip and, if allowed, paste it into the app you were in."""
        if clip_id is None or not self.watcher.copy(clip_id):
            return
        if self.on_change:
            self.on_change()
        if not self.cfg.get("clipboard_paste", True):
            return
        if not can_paste():
            self.rumps.notification("Reclaim", "Copied",
                                    "Press ⌘V to paste. Allow Reclaim under Accessibility "
                                    "to paste automatically.")
            return
        prev = self.prev_app
        self.prev_app = None

        def paste():
            if prev is not None:
                prev.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
            time.sleep(0.18)             # let focus land before the keystroke
            press_cmd_v()
        threading.Thread(target=paste, daemon=True).start()
