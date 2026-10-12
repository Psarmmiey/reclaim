"""Updates: what counts as newer, and that a bad download never replaces the app."""
import hashlib
import io
import json
import os
import plistlib
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeDaemon:
    def __init__(self):
        self.revs = 0

    def touch(self):
        self.revs += 1


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def app_zip(version, bundle_id="com.reclaim.app"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Reclaim.app/Contents/Info.plist",
                   plistlib.dumps({"CFBundleIdentifier": bundle_id,
                                   "CFBundleShortVersionString": version}))
        z.writestr("Reclaim.app/Contents/MacOS/Reclaim", "new build")
    return buf.getvalue()


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.environ["HOME"] = self.home
        for mod in list(sys.modules):
            if mod.startswith("reclaim"):
                del sys.modules[mod]
        from reclaim import launchd, updater
        self.u, self.launchd = updater, launchd
        self.current = __import__("reclaim").__version__
        # An installed app to update.
        self.apps = os.path.join(self.home, "Applications")
        self.app = os.path.join(self.apps, "Reclaim.app")
        os.makedirs(os.path.join(self.app, "Contents", "MacOS"))
        with open(os.path.join(self.app, "Contents", "MacOS", "Reclaim"), "w") as f:
            f.write("old build")
        sys.frozen = "macosx_app"
        os.environ["RESOURCEPATH"] = os.path.join(self.app, "Contents", "Resources")
        self.ran = []
        launchd.is_installed = lambda: True

    def tearDown(self):
        del sys.frozen
        os.environ.pop("RESOURCEPATH", None)
        shutil.rmtree(self.home, ignore_errors=True)

    # ------------------------------------------------------------ fakes
    def serve(self, version="9.9.9", payload=None, sha=None, assets=True):
        payload = payload if payload is not None else app_zip(version)
        sha = sha or hashlib.sha256(payload).hexdigest()
        rel = {"tag_name": "v" + version, "html_url": "https://example/rel",
               "published_at": "2026-10-12T00:00:00Z",
               "assets": [{"name": "Reclaim-macOS.zip", "size": len(payload),
                           "browser_download_url": "zip"},
                          {"name": "Reclaim-macOS.zip.sha256", "browser_download_url": "sha"}]
               if assets else []}

        def opener(url, timeout=15):
            if url == self.u.API:
                return Resp(json.dumps(rel).encode())
            if url == "zip":
                return Resp(payload)
            if url == "sha":
                return Resp(("%s  Reclaim-macOS.zip\n" % sha).encode())
            raise AssertionError(url)
        return opener

    def runner(self, cmd, **kw):
        self.ran.append(cmd)

        class R:
            returncode, stdout, stderr = 0, "", ""
        if cmd[0].endswith("ditto"):
            zipfile.ZipFile(cmd[3]).extractall(cmd[4])
        if cmd[0] == "security":
            R.stdout = ""                       # no local certificate
        return R()

    def updater(self, **kw):
        return self.u.Updater(FakeDaemon(), opener=self.serve(**kw), runner=self.runner)

    def old_app_intact(self):
        with open(os.path.join(self.app, "Contents", "MacOS", "Reclaim")) as f:
            return f.read() == "old build"

    # ------------------------------------------------------------ tests
    def test_version_order(self):
        newer = self.u.is_newer
        self.assertTrue(newer("0.10.0", "0.9.9"))
        self.assertTrue(newer("v1.0.0", "0.99.0"))
        self.assertFalse(newer("0.3.0", "0.3.0"))
        self.assertFalse(newer("garbage", "0.1.0"))

    def test_check_finds_newer_release(self):
        up = self.updater()
        rel = up.check()
        self.assertEqual(rel["version"], "9.9.9")
        self.assertEqual(up.state["available"]["tag"], "v9.9.9")

    def test_check_ignores_same_version(self):
        up = self.updater(version=self.current)
        self.assertIsNone(up.check())

    def test_release_without_mac_build_is_not_offered(self):
        up = self.updater(assets=False)
        self.assertIsNone(up.check())
        self.assertIn("no Mac build", up.state["error"])

    def test_offline_is_reported_not_raised(self):
        def offline(url, timeout=15):
            raise OSError("network is down")
        up = self.u.Updater(FakeDaemon(), opener=offline, runner=self.runner)
        self.assertIsNone(up.check())
        self.assertIn("could not reach GitHub", up.state["error"])

    def test_install_swaps_keeps_previous_and_restarts(self):
        up = self.updater()
        up.check()
        up.install()
        with open(os.path.join(self.app, "Contents", "MacOS", "Reclaim")) as f:
            self.assertEqual(f.read(), "new build")
        with open(os.path.join(self.app + ".previous", "Contents", "MacOS", "Reclaim")) as f:
            self.assertEqual(f.read(), "old build")
        self.assertTrue(any(c[:3] == ["launchctl", "kickstart", "-k"] for c in self.ran))

    def test_corrupt_download_never_replaces_the_app(self):
        up = self.updater(sha="0" * 64)
        up.check()
        with self.assertRaises(self.u.UpdateError):
            up.install()
        self.assertTrue(self.old_app_intact())
        self.assertFalse(os.path.exists(self.app + ".previous"))
        self.assertIn("checksum", up.state["error"])

    def test_wrong_app_is_refused(self):
        up = self.updater(payload=app_zip("9.9.9", bundle_id="com.evil.app"))
        up.check()
        with self.assertRaises(self.u.UpdateError):
            up.install()
        self.assertTrue(self.old_app_intact())

    def test_wrong_version_inside_is_refused(self):
        up = self.updater(payload=app_zip("1.0.0"))
        up.check()
        with self.assertRaises(self.u.UpdateError):
            up.install()
        self.assertTrue(self.old_app_intact())

    def test_resigns_with_local_certificate_when_present(self):
        up = self.updater()
        cert = "A" * 40

        def runner(cmd, **kw):
            r = self.runner(cmd, **kw)
            if cmd[0] == "security":
                r.stdout = '  1) %s "Reclaim Local Signing"\n' % cert
            return r
        up.run = runner
        self.u.subprocess.run = runner
        try:
            up.check()
            up.install()
        finally:
            import subprocess
            self.u.subprocess.run = subprocess.run
        sign = [c for c in self.ran if c[0].endswith("codesign")]
        self.assertEqual(sign[0][4], cert)

    def test_source_installs_are_told_to_use_git(self):
        del sys.frozen
        up = self.updater()
        up.check()
        with self.assertRaises(self.u.UpdateError) as e:
            up.install()
        self.assertIn("git pull", str(e.exception))
        sys.frozen = "macosx_app"


if __name__ == "__main__":
    unittest.main()
