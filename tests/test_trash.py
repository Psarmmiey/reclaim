"""Trash policy: when an undo copy is kept, and that purging honours it."""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GB = 1_000_000_000


class TrashTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.environ["HOME"] = self.home
        for mod in list(sys.modules):
            if mod.startswith("reclaim"):
                del sys.modules[mod]
        from reclaim import clean, paths, rules, store
        self.clean, self.paths, self.rules = clean, paths, rules
        self.con = store.connect()
        self.cfg = {"undo_days": 7, "alert_below_gb": 25, "trash_policy": "auto"}

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.home, ignore_errors=True)

    def item(self, safety="safe", source="rules"):
        return {"safety": safety, "source": source}

    def test_auto_deletes_rule_caches_when_space_is_low(self):
        self.assertFalse(self.clean.should_trash(self.item(), self.cfg, 10 * GB))

    def test_auto_keeps_undo_when_space_is_fine(self):
        self.assertTrue(self.clean.should_trash(self.item(), self.cfg, 80 * GB))

    def test_auto_always_trashes_review_items(self):
        it = self.item(safety="review")
        self.assertTrue(self.clean.should_trash(it, self.cfg, 1 * GB))

    def test_auto_always_trashes_ai_sourced_items(self):
        it = self.item(source="claude")
        self.assertTrue(self.clean.should_trash(it, self.cfg, 1 * GB))

    def test_explicit_policies(self):
        for policy, want in (("always", True), ("ask", True), ("never", False)):
            self.cfg["trash_policy"] = policy
            self.assertEqual(self.clean.should_trash(self.item(), self.cfg, 1 * GB),
                             want, policy)

    def test_undo_days_zero_never_trashes(self):
        self.cfg.update(undo_days=0, trash_policy="always")
        self.assertFalse(self.clean.should_trash(self.item(), self.cfg, 80 * GB))

    def mkcache(self, name):
        p = os.path.join(self.home, "proj", name, "node_modules")
        os.makedirs(p)
        with open(os.path.join(p, "x"), "w") as f:
            f.write("x" * 100)
        old = time.time() - 200 * 86400            # untouched for months
        os.utime(p, (old, old))
        return {"path": p, "bytes": 100, "label": "npm"}

    def test_accounting_separates_freed_from_trashed(self):
        a, b = self.mkcache("a"), self.mkcache("b")
        res = self.clean.remove_many(self.con, [a, b],
                                     trash=lambda it: it is a)
        self.assertEqual(res["trashed_bytes"], 100)
        self.assertEqual(res["freed_bytes"], 100)
        self.assertFalse(os.path.exists(b["path"]))
        self.assertEqual(len(os.listdir(self.paths.TRASH_DIR)), 1)

    def test_purge_uses_trash_time_not_folder_mtime(self):
        a = self.mkcache("a")
        self.clean.remove_many(self.con, [a], trash=True)
        self.assertEqual(self.clean.purge_trash(self.con, 7), 0)
        self.assertEqual(len(os.listdir(self.paths.TRASH_DIR)), 1)

    def test_purge_removes_expired_items(self):
        a = self.mkcache("a")
        self.clean.remove_many(self.con, [a], trash=True)
        name = os.listdir(self.paths.TRASH_DIR)[0]
        old = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() - 8 * 86400))
        os.rename(os.path.join(self.paths.TRASH_DIR, name),
                  os.path.join(self.paths.TRASH_DIR, old + name[15:]))
        self.assertGreater(self.clean.purge_trash(self.con, 7), 0)
        self.assertEqual(os.listdir(self.paths.TRASH_DIR), [])

    def test_empty_trash_removes_everything(self):
        self.clean.remove_many(self.con, [self.mkcache("a")], trash=True)
        self.assertGreater(self.clean.purge_trash(self.con, 0), 0)
        self.assertEqual(os.listdir(self.paths.TRASH_DIR), [])


if __name__ == "__main__":
    unittest.main()


class EmptyInBackgroundTests(TrashTests):
    def daemon(self):
        from reclaim.daemon import Daemon
        return Daemon(dict(self.cfg, scan_root=self.home), self.con)

    def wait(self, d):
        end = time.time() + 10
        while d.emptying and time.time() < end:
            time.sleep(0.05)
        self.assertIsNone(d.emptying)

    def test_trash_looks_empty_at_once_and_is_deleted_in_background(self):
        self.clean.remove_many(self.con, [self.mkcache("a"), self.mkcache("b")], trash=True)
        d = self.daemon()
        self.assertTrue(d.empty_trash_async())
        self.assertEqual(os.listdir(self.paths.TRASH_DIR), [])     # swapped instantly
        self.assertEqual(d.trash_bytes(), 0)
        self.wait(d)
        self.assertEqual(d.empty_done[-1]["freed_bytes"], 200)
        self.assertEqual(self.clean.pending_purges(), [])

    def test_second_request_while_emptying_is_refused(self):
        self.clean.remove_many(self.con, [self.mkcache("a")], trash=True)
        d = self.daemon()
        d.emptying = {"total_bytes": 1, "freed_bytes": 0, "started_at": 0}
        self.assertFalse(d.empty_trash_async())
        self.assertEqual(len(os.listdir(self.paths.TRASH_DIR)), 1)

    def test_nothing_to_empty(self):
        self.assertFalse(self.daemon().empty_trash_async())

    def test_interrupted_purge_resumes(self):
        self.clean.remove_many(self.con, [self.mkcache("a")], trash=True)
        left = self.clean.detach_trash()                 # as if the app died here
        self.assertEqual(self.clean.pending_purges(), [left])
        d = self.daemon()
        d.resume_purges()
        self.wait(d)
        self.assertFalse(os.path.exists(left))

    def test_delete_detached_refuses_anything_else(self):
        other = os.path.join(self.home, "proj")
        os.makedirs(other)
        with self.assertRaises(self.clean.Refused):
            self.clean.delete_detached(other)
        with self.assertRaises(self.clean.Refused):
            self.clean.delete_detached(self.paths.TRASH_DIR)
        self.assertTrue(os.path.isdir(other))
