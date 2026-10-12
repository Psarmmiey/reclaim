"""The menu bar half. macOS only; needs `rumps`.

Runs the daemon and the local server in background threads and puts free space
in the menu bar, so one process is the whole app.
"""
import subprocess
import sys
import threading
import webbrowser

from . import clean, config, format as fmt, server, store, version_string


def _alert(**kw):
    """rumps.alert, brought to the front first. A menu bar app is never the
    active app, so otherwise the dialog can open behind other windows."""
    import rumps
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass
    return rumps.alert(**kw)


def _menu_bar_only():
    """No Dock icon. A bundled app gets this from LSUIElement in Info.plist;
    run as plain `python3 -m reclaim`, macOS shows a Python icon in the Dock
    unless the activation policy is set to accessory before the app runs."""
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory)
    except Exception:
        pass


def _lenient(timer, seconds):
    """Allow macOS to fire a rumps.Timer up to `seconds` late, batching it
    with other wake-ups instead of waking the CPU just for us."""
    try:
        timer._nstimer.setTolerance_(seconds)
    except Exception:
        pass


def _require_rumps():
    try:
        import rumps                                    # noqa: F401
        return True
    except ImportError:
        return False


def run(daemon=None):
    if sys.platform != "darwin":
        raise SystemExit("The menu bar app only runs on macOS. "
                         "Use `reclaim daemon` instead.")
    if not _require_rumps():
        raise SystemExit("rumps is not installed. Run: pip install rumps")

    import rumps
    from .daemon import Daemon

    cfg = config.load()
    d = daemon or Daemon(cfg)

    class ReclaimApp(rumps.App):
        def __init__(self):
            super().__init__("Reclaim", title="--", quit_button=None)
            self.daemon = d
            self.hogs = []
            self.menu = [
                rumps.MenuItem("Free space", callback=None),
                None,
                rumps.MenuItem("Biggest items", callback=None),
                rumps.MenuItem("Clipboard", callback=None),
                rumps.MenuItem("Running apps", callback=None),
                None,
                rumps.MenuItem("Scan now", callback=self.on_scan),
                rumps.MenuItem("Clean safe caches", callback=self.on_clean),
                rumps.MenuItem("Show Reclaim trash", callback=self.on_show_trash),
                rumps.MenuItem("Empty Reclaim trash", callback=self.on_empty),
                None,
                rumps.MenuItem("Open dashboard", callback=self.on_dashboard),
                rumps.MenuItem("Tools...", callback=self.on_tools),
                rumps.MenuItem("Uninstall an app...", callback=self.on_uninstall),
                rumps.MenuItem("Reveal biggest in Finder", callback=self.on_reveal),
                None,
                rumps.MenuItem("Check for updates...", callback=self.on_update),
                rumps.MenuItem("Start at login", callback=self.on_login_item),
                rumps.MenuItem("Reclaim %s" % version_string(), callback=None),
                rumps.MenuItem("Quit Reclaim", callback=self.on_quit),
            ]
            self.seen_rev = -1
            self._redraw_pending = False
            self._pending_note = None
            d.on_touch.append(self._touched)
            # Free space drifts without events, so that one still ticks; the
            # tolerance lets macOS fold it into wake-ups it is doing anyway.
            self.refresher = rumps.Timer(self.refresh, 30)
            self.refresher.start()
            _lenient(self.refresher, 10)
            from . import launchd as _ld
            self.menu["Start at login"].state = 1 if _ld.is_installed() else 0
            self.clip = None
            if d.cfg.get("clipboard", True):
                try:
                    from .clipboard_mac import Clipboard
                    from . import store as _store
                    self.clip = Clipboard(lambda: d.cfg, on_change=d.touch,
                                          log=lambda m: _store.log_event(d.con, "info", m))
                    d.on_config.append(self._config_changed)
                    d.clipboard = self.clip.watcher
                except Exception as e:                # never take the app down
                    from . import store
                    store.log_event(d.con, "error", "Clipboard history off: %s" % e)
            self.apps = None
            try:
                from . import running
                self.apps = running.backend()
                running.observe_launches(self._apps_changed)
            except Exception as e:
                from . import store
                store.log_event(d.con, "error", "Running apps list off: %s" % e)
            self.refresh(None)
            self.refresh_clips()
            if self.clip:
                self.clip.start()

        # Redraws are event-driven: any change anywhere calls daemon.touch(),
        # which schedules one redraw on the main thread. No polling timer.

        def _touched(self):
            if self._redraw_pending:
                return                       # a redraw is already on its way
            self._redraw_pending = True
            from PyObjCTools import AppHelper
            AppHelper.callAfter(self._redraw)

        def _redraw(self):
            self._redraw_pending = False
            note, self._pending_note = self._pending_note, None
            if note:
                rumps.notification("Reclaim", note, "")
            while self.daemon.empty_done:
                done = self.daemon.empty_done.pop(0)
                rumps.notification("Reclaim", "Freed %s" % fmt.human(done["freed_bytes"]),
                                   "Reclaim trash emptied.")
            if self.daemon.rev != self.seen_rev:
                self.seen_rev = self.daemon.rev
                self.refresh(None)
                self.refresh_clips()

        # ------------------------------------------------------- running apps

        def _apps_changed(self):
            from PyObjCTools import AppHelper
            AppHelper.callAfter(self.refresh_running)

        def refresh_running(self):
            from . import running
            parent = self.menu["Running apps"]
            if parent._menu is not None:
                parent.clear()
            if not self.apps:
                parent.add(rumps.MenuItem("Not available"))
                return
            rows = running.snapshot(self.apps)
            for a in rows[:30]:
                title = "%s  %s" % (a["name"], fmt.human(a["memory"]))
                item = rumps.MenuItem(title)
                item.add(rumps.MenuItem("Show", callback=self._app_cb("show", a)))
                if not a["protected"]:
                    item.add(rumps.MenuItem("Quit", callback=self._app_cb("quit", a)))
                    item.add(rumps.MenuItem("Force Quit...", callback=self._app_cb("force", a)))
                parent.add(item)
            if not rows:
                parent.add(rumps.MenuItem("Nothing open"))
            parent.add(rumps.separator)
            parent.add(rumps.MenuItem("Open in dashboard...", callback=lambda _: webbrowser.open(
                server.url(self.daemon.cfg) + "#tools/running")))

        def _app_cb(self, action, app):
            def cb(_):
                if action == "show":
                    self.apps.show(app["pid"])
                    return
                if action == "force" and not _alert(
                        title="Force %s to quit?" % app["name"],
                        message="You will lose any unsaved changes.",
                        ok="Force Quit", cancel="Cancel"):
                    return
                if not self.apps.quit(app["pid"], force=(action == "force")):
                    self._note("%s did not quit. Try Force Quit." % app["name"])
            return cb

        def _note(self, text):
            """Show a notification from any thread."""
            self._pending_note = text
            self.daemon.touch()

        # ------------------------------------------------------- clipboard

        def refresh_clips(self):
            parent = self.menu["Clipboard"]
            if parent._menu is not None:
                parent.clear()
            if not self.clip:
                parent.add(rumps.MenuItem("Clipboard history is off"))
                return
            if not self.clip.trusted:
                parent.add(rumps.MenuItem("Turn on the keyboard shortcut...",
                                          callback=lambda _: self.clip.open_accessibility_settings()))
                parent.add(rumps.separator)
            for item in self.clip.build_menu(limit=12, numbered=False):
                parent.add(item)
            parent.add(rumps.separator)
            hk = self.daemon.cfg.get("clipboard_hotkey", "ctrl+cmd+v")
            parent.add(rumps.MenuItem("Show all... (%s)" % hk.replace("+", " ").upper(),
                                      callback=self.on_clip_all))
            paused = self.daemon.cfg.get("clipboard_paused")
            parent.add(rumps.MenuItem("Resume recording" if paused else "Pause recording",
                                      callback=self.on_clip_pause))
            parent.add(rumps.MenuItem("Clear history...", callback=self.on_clip_clear))

        def _config_changed(self):
            """Called from the server thread; AppKit work must hop to main."""
            from PyObjCTools import AppHelper

            def apply():
                if self.clip:
                    self.clip.install_hotkey()
                self.refresh_clips()
            AppHelper.callAfter(apply)

        def on_clip_all(self, _):
            webbrowser.open(server.url(self.daemon.cfg) + "#clipboard")

        def on_clip_pause(self, _):
            paused = not self.daemon.cfg.get("clipboard_paused")
            self.daemon.cfg = config.set_values(clipboard_paused=paused)
            self.refresh_clips()

        def on_clip_clear(self, _):
            if _alert(title="Clear clipboard history?",
                      message="Everything except pinned items is deleted.",
                      ok="Clear", cancel="Cancel"):
                from . import clipboard
                clipboard.clear(self.clip.watcher.con)
                self.refresh_clips()

        # ------------------------------------------------------- refresh

        def refresh(self, _sender):
            try:
                s = self.daemon.status()
            except Exception:
                return
            if _sender is not None:          # the 30 s tick, not an event redraw
                self.refresh_running()
            mark = {"ok": "", "low": " ⚠︎", "critical": " ⚠︎"}[s["level"]]
            self.title = "%.0fGB%s" % (fmt.gb(s["free_bytes"]), mark)
            self.menu["Free space"].title = "%s free of %s" % (
                fmt.human(s["free_bytes"]), fmt.human(s["disk_bytes"]))
            if s["safe_count"]:
                self.menu["Clean safe caches"].title = (
                    "Clean safe caches (%s)" % fmt.human(s["safe_bytes"]))
                self.menu["Clean safe caches"].set_callback(self.on_clean)
            else:
                self.menu["Clean safe caches"].title = "Nothing to auto-clean"
                self.menu["Clean safe caches"].set_callback(None)
            up = s.get("update") or {}
            upd = self.menu["Check for updates..."]
            if up.get("installing"):
                upd.title = "Updating: %s" % (up.get("progress") or "...")
            elif up.get("checking"):
                upd.title = "Checking for updates..."
            elif up.get("available"):
                upd.title = "Update to %s..." % up["available"]["version"]
            elif up.get("error"):
                upd.title = "Check for updates... (last check failed)"
            else:
                upd.title = "Check for updates..."
            item = self.menu["Empty Reclaim trash"]
            e = s["emptying"]
            if e:
                item.title = ("Emptying trash... %s of %s freed"
                              % (fmt.human(e["freed_bytes"]), fmt.human(e["total_bytes"]))
                              if e["total_bytes"] else "Emptying trash...")
                item.set_callback(None)
            elif s["trash_bytes"]:
                item.title = "Empty Reclaim trash (%s)" % fmt.human(s["trash_bytes"])
                item.set_callback(self.on_empty)
            else:
                item.title = "Reclaim trash is empty"
                item.set_callback(None)
            self.menu["Scan now"].title = (
                "Scanning..." if s["scanning"] else "Scan now")
            self._refresh_hogs()

        def _refresh_hogs(self):
            last = store.latest_scan(self.daemon.con)
            top = store.targets_for(self.daemon.con, last["id"], limit=5) if last else []
            self.hogs = top
            parent = self.menu["Biggest items"]
            # rumps only creates the submenu when the first child is added.
            if parent._menu is not None:
                parent.clear()
            if not top:
                parent.add(rumps.MenuItem("No scan yet"))
                return
            for t in top:
                item = rumps.MenuItem(
                    "%s  %s" % (fmt.human(t["bytes"]), t["label"]),
                    callback=self.on_reveal_item)
                item.path = t["path"]
                parent.add(item)

        # ------------------------------------------------------- actions

        def on_scan(self, _):
            threading.Thread(target=self.daemon.scan_now, daemon=True).start()
            rumps.notification("Reclaim", "Scanning", "This runs in the background.")

        def on_clean(self, _):
            s = self.daemon.status()
            size = fmt.human(s["safe_bytes"])
            days = self.daemon.cfg.get("undo_days", 7)
            base = ("%s of caches. Everything here rebuilds itself the next "
                    "time you build or launch the app." % size)
            if s["trash_policy"] == "ask" and days:
                # rumps.alert: 1 = ok, 0 = cancel, -1 = other
                r = _alert(
                    title="Clean safe caches?",
                    message=base + "\n\nDelete now frees the space straight "
                            "away. Move to trash keeps an undo for %d days, but "
                            "the space only comes back when the trash is "
                            "emptied." % days,
                    ok="Delete now", cancel="Cancel", other="Move to trash")
                if r == 0:
                    return
                trash = (r == -1)
            else:
                plan = self.daemon.trash_decider()
                items = self._safe_items()
                kept = [t for t in items
                        if (plan(t) if callable(plan) else plan)]
                if not kept:
                    how = "They will be deleted straight away."
                elif len(kept) == len(items):
                    how = ("They go to Reclaim trash for %d days, so the space "
                           "comes back when it is emptied." % days)
                else:
                    how = "Some are deleted now, the rest go to Reclaim trash."
                if not _alert(title="Clean safe caches?",
                                   message=base + "\n\n" + how,
                                   ok="Clean", cancel="Cancel"):
                    return
                trash = None
            res = self.daemon.clean_safe(mode="manual", trash=trash)
            msg = "Freed %s" % fmt.human(res["freed_bytes"])
            if res["trashed_bytes"]:
                msg += ", %s in trash" % fmt.human(res["trashed_bytes"])
            rumps.notification("Reclaim", msg, "%d items removed." % len(res["removed"]))
            self.refresh(None)

        def _safe_items(self):
            last = store.latest_scan(self.daemon.con)
            if not last:
                return []
            return clean.auto_cleanable(
                store.targets_for(self.daemon.con, last["id"]))

        def on_empty(self, _):
            s = self.daemon.status()
            if not s["trash_bytes"] or s["emptying"]:
                return
            if not _alert(
                    title="Empty Reclaim trash?",
                    message="Frees %s. These items can no longer be restored. "
                            "It runs in the background; you'll get a "
                            "notification when it's done."
                            % fmt.human(s["trash_bytes"]),
                    ok="Empty", cancel="Cancel"):
                return
            if self.daemon.empty_trash_async():
                self.refresh(None)

        def on_dashboard(self, _):
            webbrowser.open(server.url(self.daemon.cfg))

        def on_tools(self, _):
            webbrowser.open(server.url(self.daemon.cfg) + "#tools")

        def on_uninstall(self, _):
            webbrowser.open(server.url(self.daemon.cfg) + "#tools/uninstaller")

        def on_show_trash(self, _):
            webbrowser.open(server.url(self.daemon.cfg) + "#trash")

        def on_reveal(self, _):
            if self.hogs:
                subprocess.run(["open", "-R", self.hogs[0]["path"]], check=False)

        def on_reveal_item(self, sender):
            subprocess.run(["open", "-R", getattr(sender, "path", "")], check=False)

        def on_update(self, _):
            up = self.daemon.updater
            rel = up.state.get("available")
            if not rel:
                def check():
                    found = up.check()
                    self._note(("Reclaim %s is out" % found["version"]) if found
                               else (up.state.get("error") or "You have the latest version"))
                threading.Thread(target=check, daemon=True).start()
                return
            if not _alert(title="Update to Reclaim %s?" % rel["version"],
                          message="Reclaim downloads it from GitHub, checks it, swaps it in and "
                                  "restarts. The current version is kept so you can roll back.",
                          ok="Update", cancel="Not now"):
                return

            def go():
                try:
                    up.install()
                except Exception as e:
                    self._note("Update failed: %s" % e)
            threading.Thread(target=go, daemon=True).start()

        def on_login_item(self, sender):
            from . import bundle, launchd
            if launchd.is_installed():
                launchd.uninstall()
            else:
                problem = bundle.install_problem()
                if problem:
                    _alert(title="Not yet", message=problem, ok="OK")
                    return
                ok, msg = launchd.install(mode="app")
                if not ok:
                    _alert(title="Could not add the login item", message=msg, ok="OK")
            sender.state = 1 if launchd.is_installed() else 0

        def on_quit(self, _):
            self.daemon.stop()
            rumps.quit_application()

    server.serve_in_thread(d, cfg)
    threading.Thread(target=d.run_forever, daemon=True).start()
    _menu_bar_only()
    ReclaimApp().run()
