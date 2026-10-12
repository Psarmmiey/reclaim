"""Clipboard history: privacy rules, de-duplication, limits."""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakePasteboard:
    def __init__(self):
        self.n, self.t, self.s, self.f, self.png, self.app = 0, [], None, [], None, "Notes"

    def put(self, text=None, files=None, png=None, types=(), app="Notes"):
        self.n += 1
        self.s, self.f, self.png, self.app = text, files or [], png, app
        self.t = list(types) + (["public.utf8-plain-text"] if text else [])

    def change_count(self): return self.n
    def types(self): return self.t
    def string(self): return self.s
    def file_paths(self): return self.f
    def image_png(self): return self.png
    def frontmost_app(self): return self.app

    def write(self, text=None, files=None, png=None):
        self.put(text=text, files=files, png=png)


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.environ["HOME"] = self.home
        for mod in list(sys.modules):
            if mod.startswith("reclaim"):
                del sys.modules[mod]
        from reclaim import clipboard
        self.cb = clipboard
        self.pb = FakePasteboard()
        self.w = clipboard.Watcher(self.pb, {"clipboard": True})

    def tearDown(self):
        self.w.con.close()
        shutil.rmtree(self.home, ignore_errors=True)

    def texts(self):
        return [c["text"] for c in self.cb.recent(self.w.con)]

    def test_records_text_newest_first(self):
        for t in ("one", "two", "three"):
            self.pb.put(text=t)
            self.w.poll()
        self.assertEqual(self.texts(), ["three", "two", "one"])

    def test_nothing_new_means_nothing_recorded(self):
        self.pb.put(text="a")
        self.w.poll()
        self.w.poll()
        self.assertEqual(self.texts(), ["a"])

    def test_password_manager_copies_are_never_stored(self):
        self.pb.put(text="hunter2", types=["org.nspasteboard.ConcealedType"])
        self.w.poll()
        self.pb.put(text="s3cret", app="1Password")
        self.w.poll()
        self.pb.put(text="otp", types=["org.nspasteboard.TransientType"])
        self.w.poll()
        self.assertEqual(self.texts(), [])

    def test_repeat_moves_to_top_instead_of_duplicating(self):
        for t in ("a", "b", "a"):
            self.pb.put(text=t)
            self.w.poll()
        self.assertEqual(self.texts(), ["a", "b"])

    def test_files_beat_their_names_as_text(self):
        self.pb.put(text="report.pdf", files=["/Users/x/report.pdf"])
        self.w.poll()
        c = self.cb.recent(self.w.con)[0]
        self.assertEqual((c["kind"], c["text"]), ("files", "/Users/x/report.pdf"))

    def test_images_are_kept_and_huge_ones_skipped(self):
        self.pb.put(png=b"\x89PNG" + b"x" * 100)
        self.w.poll()
        self.pb.put(png=b"\x89PNG" + b"x" * (self.cb.MAX_IMAGE + 1))
        self.w.poll()
        kinds = [c["kind"] for c in self.cb.recent(self.w.con)]
        self.assertEqual(kinds, ["image"])

    def test_paused_records_nothing(self):
        self.w.cfg["clipboard_paused"] = True
        self.pb.put(text="private")
        self.w.poll()
        self.assertEqual(self.texts(), [])

    def test_copying_back_does_not_record_twice(self):
        self.pb.put(text="first")
        self.w.poll()
        self.pb.put(text="second")
        self.w.poll()
        first = next(c for c in self.cb.recent(self.w.con) if c["text"] == "first")
        self.w.request_copy(first["id"])
        self.w.poll()                       # performs the copy
        self.w.poll()                       # sees its own change, ignores it
        self.assertEqual(self.pb.s, "first")
        self.assertEqual(self.texts(), ["first", "second"])

    def test_prune_keeps_pinned_and_recent(self):
        now = time.time()
        old = self.cb.add(self.w.con, "text", "old", now=now - 40 * 86400)
        pinned = self.cb.add(self.w.con, "text", "keep me", now=now - 90 * 86400)
        self.cb.set_pinned(self.w.con, pinned, True)
        for i in range(10):
            self.cb.add(self.w.con, "text", "n%d" % i, now=now - i)
        self.cb.prune(self.w.con, max_items=5, days=30, now=now)
        left = self.texts()
        self.assertIn("keep me", left)
        self.assertNotIn("old", left)
        self.assertEqual(len([t for t in left if t.startswith("n")]), 5)
        self.assertEqual(left[0], "keep me")              # pinned first

    def test_search(self):
        for t in ("git push origin main", "100% done", "hello"):
            self.cb.add(self.w.con, "text", t)
        self.assertEqual([c["text"] for c in self.cb.recent(self.w.con, query="push")],
                         ["git push origin main"])
        self.assertEqual([c["text"] for c in self.cb.recent(self.w.con, query="%")],
                         ["100% done"])

    def test_title(self):
        self.assertEqual(self.cb.title({"kind": "text", "text": "a\n  b\tc"}), "a b c")
        long = self.cb.title({"kind": "text", "text": "x" * 100}, width=10)
        self.assertEqual(len(long), 10)
        files = {"kind": "files", "text": "/a/1.txt\n/a/2.txt\n/a/3.txt\n/a/4.txt"}
        self.assertEqual(self.cb.title(files), "1.txt and 3 more")

    def test_database_is_private(self):
        mode = os.stat(self.cb.DB_FILE).st_mode & 0o777
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()


class FolderTests(unittest.TestCase):
    def test_data_folder_is_owner_only(self):
        home = tempfile.mkdtemp()
        os.environ["HOME"] = home
        for mod in list(sys.modules):
            if mod.startswith("reclaim"):
                del sys.modules[mod]
        from reclaim import paths
        paths.ensure_dirs()
        self.assertEqual(os.stat(paths.DATA_DIR).st_mode & 0o777, 0o700)
        shutil.rmtree(home, ignore_errors=True)


class HotkeyTests(unittest.TestCase):
    def test_parse(self):
        from reclaim.clipboard import parse_hotkey, MODS
        self.assertEqual(parse_hotkey("ctrl+cmd+v"), (9, MODS["ctrl"] | MODS["cmd"]))
        self.assertEqual(parse_hotkey("Shift + Cmd + C"), (8, MODS["shift"] | MODS["cmd"]))
        self.assertIsNone(parse_hotkey("v"))             # would fire while typing
        self.assertIsNone(parse_hotkey("shift+v"))       # same
        self.assertIsNone(parse_hotkey("cmd+F5"))        # unknown key
        self.assertIsNone(parse_hotkey("hyper+v"))       # unknown modifier
