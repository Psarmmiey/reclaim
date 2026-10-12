"""Tools: what they find, and what they refuse to touch."""
import os
import plistlib
import shutil
import stat
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.environ["HOME"] = self.home
        for mod in list(sys.modules):
            if mod.startswith("reclaim"):
                del sys.modules[mod]
        from reclaim import clean, paths, store, tools
        from reclaim.tools import ai, apps, devjunk, files
        tools.load_all()
        # Never reach the real system: no xcrun, brew, docker or ollama, and
        # launchctl/osascript stubs that answer instantly (a CI runner's real
        # osascript can sit on a permission prompt).
        self.fakebin = os.path.join(self.home, "bin")
        os.makedirs(self.fakebin)
        for name, out in (("launchctl", ""), ("osascript", "[]")):
            with open(os.path.join(self.fakebin, name), "w") as f:
                f.write("#!/bin/sh\necho '%s'\nexit 0\n" % out)
            os.chmod(os.path.join(self.fakebin, name), 0o755)
        self._path = os.environ["PATH"]
        os.environ["PATH"] = self.fakebin + os.pathsep + self._path
        devjunk._which = ai._which = lambda name: None
        self.clean, self.paths, self.tools, self.apps, self.files = clean, paths, tools, apps, files
        self.con = store.connect()
        # A private Applications folder instead of the real /Applications.
        self.appdir = os.path.join(self.home, "Applications")
        os.makedirs(self.appdir)
        clean.APP_DIRS = (self.appdir,)
        apps.app_dirs = lambda: [self.appdir]
        apps.known_bundle_ids = lambda: {
            apps._plist(p).get("CFBundleIdentifier", "").lower()
            for p in apps.installed_apps()}
        from reclaim.daemon import Daemon
        self.d = Daemon({"undo_days": 7, "alert_below_gb": 25, "trash_policy": "auto",
                         "scan_root": self.home}, self.con)

    def tearDown(self):
        os.environ["PATH"] = self._path
        self.con.close()
        shutil.rmtree(self.home, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    def mkapp(self, name, bid):
        p = os.path.join(self.appdir, name + ".app", "Contents")
        os.makedirs(os.path.join(p, "MacOS"))
        with open(os.path.join(p, "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": bid, "CFBundleName": name}, f)
        with open(os.path.join(p, "MacOS", name), "w") as f:
            f.write("x" * 1000)
        return os.path.dirname(p)

    def mk(self, rel, data=b"x" * 100, dir_=False):
        p = os.path.join(self.home, rel)
        if dir_:
            os.makedirs(p, exist_ok=True)
            with open(os.path.join(p, "f"), "wb") as f:
                f.write(data)
        else:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
        return p

    def run_tool(self, tid, act_ids=None):
        r = self.d.tools
        self.assertTrue(r.scan(tid))
        self.wait(tid)
        st = r.state[tid]
        self.assertIsNone(st["error"], st["error"])
        if act_ids is not None:
            self.assertTrue(r.act(tid, act_ids))
            self.wait(tid)
            self.assertIsNone(st["error"], st["error"])
        return st

    def wait(self, tid):
        end = time.time() + 20
        while self.d.tools.state[tid]["state"] != "idle" and time.time() < end:
            time.sleep(0.02)

    # ------------------------------------------------------------ guards
    def test_personal_guard(self):
        doc = self.mk("Documents/big.mov")
        with self.assertRaises(self.clean.Refused):
            self.clean.guard(doc)
        self.assertEqual(self.clean.guard(doc, personal=True), doc)
        lib = self.mk("Pictures/Photos Library.photoslibrary/db.sqlite")
        with self.assertRaises(self.clean.Refused):
            self.clean.guard(lib, personal=True)
        with self.assertRaises(self.clean.Refused):           # the folder itself
            self.clean.guard(os.path.join(self.home, "Documents"), personal=True)
        key = self.mk(".ssh/id_ed25519")
        with self.assertRaises(self.clean.Refused):
            self.clean.guard(key, personal=True)

    def test_app_guard(self):
        ok = self.mkapp("Thing", "com.vendor.thing")
        self.assertEqual(self.clean.guard_app(ok), ok)
        apple = self.mkapp("Notes", "com.apple.Notes")
        with self.assertRaises(self.clean.Refused):
            self.clean.guard_app(apple)
        me = self.mkapp("Reclaim", "com.reclaim.app")
        with self.assertRaises(self.clean.Refused):
            self.clean.guard_app(me)
        elsewhere = os.path.join(self.home, "Downloads", "X.app")
        shutil.copytree(ok, elsewhere)
        with self.assertRaises(self.clean.Refused):
            self.clean.guard_app(elsewhere)

    # ------------------------------------------------------------ apps
    def test_leftovers_match_bundle_id_helpers_and_groups(self):
        L = "Library/"
        want = [self.mk(L + "Application Support/com.vendor.thing", dir_=True),
                self.mk(L + "Caches/com.vendor.thing.helper", dir_=True),
                self.mk(L + "Preferences/com.vendor.thing.plist"),
                self.mk(L + "Group Containers/ABCDE12345.com.vendor.thing", dir_=True),
                self.mk(L + "Application Support/Thing", dir_=True)]
        other = [self.mk(L + "Caches/com.vendor.thingamajig", dir_=True),
                 self.mk(L + "Preferences/com.vendor.other.plist")]
        found = self.apps.leftovers("com.vendor.thing", ["Thing"])
        self.assertEqual(sorted(found), sorted(want))
        for p in other:
            self.assertNotIn(p, found)

    def test_generic_app_names_do_not_match_folders(self):
        self.mk("Library/Application Support/Code", dir_=True)
        self.assertEqual(self.apps.leftovers("com.microsoft.VSCode", ["Code"]), [])

    def test_uninstall_removes_app_and_ticked_leftovers_only(self):
        app = self.mkapp("Thing", "com.vendor.thing")
        cache = self.mk("Library/Caches/com.vendor.thing", dir_=True)
        prefs = self.mk("Library/Preferences/com.vendor.thing.plist")
        st = self.run_tool("uninstaller")
        kids = st["items"][0]["children"]
        self.assertEqual({k["id"] for k in kids}, {app, cache, prefs})
        self.run_tool("uninstaller", [app, cache])
        self.assertFalse(os.path.exists(app))
        self.assertFalse(os.path.exists(cache))
        self.assertTrue(os.path.exists(prefs))

    def test_act_ignores_ids_that_were_not_scanned(self):
        victim = self.mk("Documents/precious.txt")
        self.run_tool("uninstaller", [victim, "/etc/hosts"])
        self.assertTrue(os.path.exists(victim))

    def test_orphan_leftovers(self):
        self.mkapp("Kept", "com.alive.kept")
        orphan = self.mk("Library/Application Support/com.gone.app", dir_=True, data=b"x" * 9000)
        alive = self.mk("Library/Caches/com.alive.kept", dir_=True)
        helper = self.mk("Library/Caches/com.alive.kept.helper", dir_=True)
        st = self.run_tool("leftovers")
        ids = {c["id"] for g in st["items"] for c in g["children"]}
        self.assertIn(orphan, ids)
        self.assertNotIn(alive, ids)
        self.assertNotIn(helper, ids)

    # ------------------------------------------------------------ files
    def test_duplicates_keep_one_copy(self):
        data = os.urandom(2 * 1024 * 1024)
        a = self.mk("Documents/a.bin", data)
        b = self.mk("Downloads/a copy.bin", data)
        c = self.mk("Movies/a.bin", data)
        self.mk("Documents/different.bin", os.urandom(2 * 1024 * 1024))
        st = self.run_tool("duplicates")
        self.assertEqual(len(st["items"]), 1)
        kids = st["items"][0]["children"]
        kept = [k for k in kids if k.get("disabled")]
        self.assertEqual(len(kept), 1)
        self.assertNotEqual(kept[0]["path"], b)            # Downloads copy never kept
        self.run_tool("duplicates", [k["id"] for k in kids])  # even ticking all
        left = [p for p in (a, b, c) if os.path.exists(p)]
        self.assertEqual(left, [kept[0]["path"]])

    def test_duplicates_refuse_when_the_kept_copy_vanished(self):
        data = os.urandom(2 * 1024 * 1024)
        a = self.mk("Documents/a.bin", data)
        b = self.mk("Downloads/b.bin", data)
        st = self.run_tool("duplicates")
        kids = st["items"][0]["children"]
        kept = next(k for k in kids if k.get("disabled"))
        os.remove(kept["path"])
        self.d.tools.act("duplicates", [k["id"] for k in kids])
        self.wait("duplicates")
        self.assertTrue(os.path.exists(a if kept["path"] == b else b))

    def test_downloads_ticks_old_installers_only(self):
        old = self.mk("Downloads/Tool.dmg", b"x" * (2 * 1024 * 1024))
        new = self.mk("Downloads/New.dmg", b"x" * (2 * 1024 * 1024))
        doc = self.mk("Downloads/report.pdf", b"x" * (2 * 1024 * 1024))
        t = time.time() - 30 * 86400
        os.utime(old, (t, t))
        st = self.run_tool("downloads")
        sel = {c["path"] for g in st["items"] for c in g["children"] if c["selected"]}
        self.assertEqual(sel, {old})
        self.run_tool("downloads", [old])
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new) and os.path.exists(doc))
        self.assertEqual(len(os.listdir(self.paths.TRASH_DIR)), 1)   # personal: trashed

    def test_large_old_never_preselects(self):
        big = self.mk("Movies/film.mov", b"x" * (101 * 1024 * 1024))
        t = time.time() - 400 * 86400
        os.utime(big, (t, t))
        st = self.run_tool("large")
        kids = [c for g in st["items"] for c in g["children"]]
        self.assertEqual([k["path"] for k in kids], [big])
        self.assertFalse(kids[0]["selected"])

    def test_dataless_icloud_files_are_skipped(self):
        p = self.mk("Documents/cloud.bin", b"x" * 2048)
        real = os.scandir
        class E:
            def __init__(s, e): s._e = e; s.name = e.name; s.path = e.path
            def is_symlink(s): return s._e.is_symlink()
            def is_dir(s, **k): return s._e.is_dir(**k)
            def stat(s, **k):
                st = s._e.stat(**k)
                if s.path == p:
                    class S: pass
                    o = S(); o.__dict__.update({a: getattr(st, a) for a in dir(st) if a.startswith("st_")})
                    o.st_flags = self.files.DATALESS
                    return o
                return st
        self.files.os.scandir = lambda d: [E(e) for e in real(d)]
        try:
            found = [q for q, _ in self.files.walk_files(self.home, 1)]
        finally:
            self.files.os.scandir = real
        self.assertNotIn(p, found)


if __name__ == "__main__":
    unittest.main()


class SmokeTests(ToolTests):
    def test_every_tool_scans_cleanly(self):
        for t in self.tools.REGISTRY:
            st = self.run_tool(t.id)
            self.assertIsInstance(st["items"], list, t.id)

    def test_background_agent_off_and_on(self):
        agent = self.mk("Library/LaunchAgents/com.vendor.updater.plist")
        with open(agent, "wb") as f:
            plistlib.dump({"Label": "com.vendor.updater",
                           "ProgramArguments": ["/nowhere/updater"]}, f)
        st = self.run_tool("background")
        item = next(c for g in st["items"] for c in g["children"] if c.get("path") == agent)
        self.assertTrue(item["selected"])                  # program missing: broken
        self.run_tool("background", [item["id"]])
        self.assertFalse(os.path.exists(agent))
        off = next(c for g in self.d.tools.state["background"]["items"]
                   for c in g["children"] if c["label"] == "com.vendor.updater")
        self.assertFalse(off["on"])
        self.run_tool("background", [off["id"]])
        self.assertTrue(os.path.exists(agent))

    def test_reclaims_own_agent_is_untouchable(self):
        ours = self.mk("Library/LaunchAgents/com.reclaim.agent.plist")
        with open(ours, "wb") as f:
            plistlib.dump({"Label": "com.reclaim.agent", "ProgramArguments": ["/x"]}, f)
        st = self.run_tool("background")
        item = next(c for g in st["items"] for c in g["children"] if c.get("path") == ours)
        self.assertTrue(item["disabled"])
        self.run_tool("background", [item["id"]])
        self.assertTrue(os.path.exists(ours))
