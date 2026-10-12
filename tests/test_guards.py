"""The tests that matter: nothing outside the sandbox ever gets deleted."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.environ["HOME"] = self.home
        for mod in list(sys.modules):
            if mod.startswith("reclaim"):
                del sys.modules[mod]
        from reclaim import clean, paths
        self.clean, self.paths = clean, paths
        self.assertTrue(paths.HOME.startswith(self.home))

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def mk(self, rel, is_dir=True):
        p = os.path.join(self.home, rel)
        os.makedirs(p if is_dir else os.path.dirname(p), exist_ok=True)
        if not is_dir:
            with open(p, "w") as f:
                f.write("x" * 64)
        return p

    def refused(self, path):
        with self.assertRaises(self.clean.Refused):
            self.clean.guard(path)

    def test_refuses_root_and_home(self):
        self.refused("/")
        self.refused(self.paths.HOME)
        self.refused(os.path.dirname(self.paths.HOME))

    def test_refuses_outside_home(self):
        other = tempfile.mkdtemp()
        try:
            self.refused(other)
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_refuses_system_paths(self):
        for p in ("/etc", "/usr/local", "/System", "/Applications", "/var"):
            self.refused(p)

    def test_refuses_protected_folders(self):
        for rel in ("Documents", "Documents/Taxes", "Pictures", ".ssh",
                    "Desktop", "Library/Mail", ".aws"):
            self.mk(rel)
            self.refused(os.path.join(self.paths.HOME, rel))

    def test_refuses_top_level_dotdir(self):
        self.mk(".gradle")
        self.refused(os.path.join(self.paths.HOME, ".gradle"))

    def test_refuses_symlink(self):
        real = self.mk("Dev/realthing")
        link = os.path.join(self.paths.HOME, "Dev", "linkthing")
        os.symlink(real, link)
        self.refused(link)

    def test_refuses_missing(self):
        self.refused(os.path.join(self.paths.HOME, "Dev", "nope"))

    def test_refuses_reclaims_own_data(self):
        os.makedirs(self.paths.DATA_DIR, exist_ok=True)
        self.refused(self.paths.DATA_DIR)

    def test_allows_a_real_cache(self):
        p = self.mk("Dev/proj/node_modules")
        self.assertEqual(self.clean.guard(p), p)
        p2 = self.mk(".gradle/caches")
        self.assertEqual(self.clean.guard(p2), p2)


class ClassifierTests(unittest.TestCase):
    """The AI may only tighten a verdict, never loosen one."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.environ["HOME"] = self.home
        for mod in list(sys.modules):
            if mod.startswith("reclaim"):
                del sys.modules[mod]
        from reclaim import classify, rules, store
        self.classify, self.rules, self.store = classify, rules, store
        self.con = store.connect()

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.home, ignore_errors=True)

    def test_ai_cannot_promote_to_safe(self):
        """Even if the model answers 'safe', the target must not become safe."""
        targets = [{"path": os.path.join(self.home, "Dev", "mystery"),
                    "bytes": 5 * 1024 ** 3, "kind": "dir", "label": "unknown",
                    "safety": self.rules.REVIEW, "source": "rules"}]
        scan_id = self.store.start_scan(self.con, self.home)
        self.store.add_targets(self.con, scan_id, targets)

        self.classify.ask_claude = lambda *a, **k: {
            targets[0]["path"]: ("safe", "totally fine, delete it")}
        self.classify.classify(self.con, scan_id, targets,
                               {"classifier": "claude"})
        # Clamped back to review, and never persisted as safe.
        self.assertEqual(targets[0]["safety"], self.rules.REVIEW)
        stored = self.store.get_verdict(self.con, targets[0]["path"])
        self.assertNotEqual(stored["safety"], self.rules.SAFE)
        rows = self.store.targets_for(self.con, scan_id)
        self.assertEqual(rows[0]["safety"], self.rules.REVIEW)

    def test_parser_drops_safe_verdicts_from_the_model(self):
        """The response parser itself refuses to accept 'safe' from Claude."""
        import subprocess
        from unittest import mock
        payload = ('[{"path":"/x","safety":"safe","reason":"go ahead"},'
                   ' {"path":"/y","safety":"keep","reason":"personal"}]')
        fake = mock.Mock(returncode=0, stdout=payload, stderr="")
        with mock.patch.object(self.classify.shutil, "which", return_value="/bin/claude"), \
             mock.patch.object(subprocess, "run", return_value=fake):
            out = self.classify.ask_claude(
                [{"path": "/x", "bytes": 1, "kind": "dir"},
                 {"path": "/y", "bytes": 1, "kind": "dir"}])
        self.assertNotIn("/x", out)
        self.assertEqual(out["/y"][0], "keep")

    def test_malformed_model_output_is_ignored(self):
        import subprocess
        from unittest import mock
        for junk in ("not json at all", "", "{}", "[1,2,3]", "[{\"path\":null}]"):
            fake = mock.Mock(returncode=0, stdout=junk, stderr="")
            with mock.patch.object(self.classify.shutil, "which", return_value="/bin/claude"), \
                 mock.patch.object(subprocess, "run", return_value=fake):
                out = self.classify.ask_claude([{"path": "/x", "bytes": 1, "kind": "dir"}])
            self.assertEqual(out, {})

    def test_ai_can_tighten_to_keep(self):
        p = os.path.join(self.home, "Dev", "thing")
        targets = [{"path": p, "bytes": 5 * 1024 ** 3, "kind": "dir",
                    "label": "unknown", "safety": self.rules.REVIEW,
                    "source": "rules"}]
        scan_id = self.store.start_scan(self.con, self.home)
        self.store.add_targets(self.con, scan_id, targets)
        self.classify.ask_claude = lambda *a, **k: {p: ("keep", "your thesis")}
        self.classify.classify(self.con, scan_id, targets, {"classifier": "claude"})
        self.assertEqual(targets[0]["safety"], self.rules.KEEP)

    def test_auto_clean_ignores_ai_sourced_targets(self):
        from reclaim import clean
        t = {"path": os.path.join(self.home, "Dev", "x"), "bytes": 1,
             "safety": self.rules.SAFE, "source": "claude"}
        self.assertEqual(clean.auto_cleanable([t]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
