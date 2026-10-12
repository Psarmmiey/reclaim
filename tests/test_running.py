"""Running Apps: memory per app tree, and what Quit and Force Quit do."""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PS = """    1     0   1000  0.0
  100     1 204800  12.5
  101   100 409600  30.0
  102   101 102400   1.5
  200     1  51200   0.2
  300     1   2048   0.0
"""


class FakeRun:
    stdout = PS
    returncode = 0


class FakeApps:
    def __init__(self):
        self.calls = []
        self.alive = {100: ("Browser", "com.example.browser"),
                      200: ("Notes", "com.apple.Notes"),
                      300: ("Reclaim", "com.reclaim.app")}

    def list(self):
        return [{"pid": p, "name": n, "bundle_id": b, "path": "", "hidden": p == 200,
                 "active": p == 100} for p, (n, b) in self.alive.items()]

    def quit(self, pid, force=False):
        self.calls.append((pid, force))
        if pid == 200 and not force:
            return False                      # Notes refuses a polite quit
        self.alive.pop(pid, None)
        return True


class RunningTests(unittest.TestCase):
    def test_helpers_count_towards_their_app(self):
        from reclaim import running
        snap = running.ps_snapshot(lambda *a, **k: FakeRun())
        mem, cpu = running.tree_usage(100, snap)
        self.assertEqual(mem, (204800 + 409600 + 102400) * 1024)
        self.assertAlmostEqual(cpu, 44.0)

    def test_snapshot_biggest_first_and_reclaim_protected(self):
        from reclaim import running
        rows = running.snapshot(FakeApps(), runner=lambda *a, **k: FakeRun())
        self.assertEqual([r["name"] for r in rows], ["Browser", "Notes", "Reclaim"])
        self.assertTrue(next(r for r in rows if r["name"] == "Reclaim")["protected"])


class RunningToolTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.environ["HOME"] = self.home
        for mod in list(sys.modules):
            if mod.startswith("reclaim"):
                del sys.modules[mod]
        from reclaim import running
        from reclaim.tools import runningapps
        from reclaim.daemon import Daemon
        running.snapshot.__defaults__ = (None, lambda *a, **k: FakeRun())
        self.apps = FakeApps()
        runningapps.BACKEND = self.apps
        runningapps.SETTLE = 0
        self.d = Daemon({"scan_root": self.home, "auto_update": "off"})

    def tearDown(self):
        self.d.con.close()
        shutil.rmtree(self.home, ignore_errors=True)

    def run_tool(self, ids=None, mode=None):
        r = self.d.tools
        (r.act("running", ids, mode) if ids is not None else r.scan("running"))
        end = time.time() + 10
        while r.state["running"]["state"] != "idle" and time.time() < end:
            time.sleep(0.02)
        st = r.state["running"]
        self.assertIsNone(st["error"], st["error"])
        return st

    def leaves(self, st):
        return {c["label"]: c for g in st["items"] for c in g["children"]}

    def test_scan_lists_apps_with_memory(self):
        apps = self.leaves(self.run_tool())
        self.assertEqual(set(apps), {"Browser", "Notes", "Reclaim"})
        self.assertTrue(apps["Reclaim"]["disabled"])
        self.assertIn("in front", apps["Browser"]["detail"])
        self.assertEqual(apps["Browser"]["bytes"], (204800 + 409600 + 102400) * 1024)

    def test_quit_asks_politely_then_force_quit_insists(self):
        apps = self.leaves(self.run_tool())
        st = self.run_tool([apps["Notes"]["id"]])
        self.assertEqual(self.apps.calls[-1], (200, False))
        self.assertEqual(len(st["result"]["failed"]), 1)          # Notes refused
        self.run_tool([apps["Notes"]["id"]], mode="force")
        self.assertEqual(self.apps.calls[-1], (200, True))
        self.assertNotIn("Notes", self.leaves(self.d.tools.state["running"]))

    def test_reclaim_cannot_be_quit_from_here(self):
        apps = self.leaves(self.run_tool())
        self.run_tool([apps["Reclaim"]["id"]], mode="force")
        self.assertNotIn((300, True), self.apps.calls)


if __name__ == "__main__":
    unittest.main()
