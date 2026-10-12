"""Inside Reclaim.app, launchd must start the bundle, not python3."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reclaim import bundle  # noqa: E402


class BundleTests(unittest.TestCase):
    def tearDown(self):
        if hasattr(sys, "frozen"):
            del sys.frozen
        os.environ.pop("RESOURCEPATH", None)

    def test_plain_install_uses_python(self):
        self.assertEqual(bundle.launch_command("app")[1:], ["-m", "reclaim", "app"])

    def test_bundle_uses_its_own_launcher(self):
        sys.frozen = "macosx_app"
        os.environ["RESOURCEPATH"] = "/Users/x/Applications/Reclaim.app/Contents/Resources"
        self.assertEqual(bundle.launch_command("app"),
                         ["/Users/x/Applications/Reclaim.app/Contents/MacOS/Reclaim", "app"])


if __name__ == "__main__":
    unittest.main()


class InstallProblemTests(unittest.TestCase):
    def tearDown(self):
        if hasattr(sys, "frozen"):
            del sys.frozen
        os.environ.pop("RESOURCEPATH", None)

    def at(self, app):
        sys.frozen = "macosx_app"
        os.environ["RESOURCEPATH"] = app + "/Contents/Resources"
        return bundle.install_problem()

    def test_translocated_or_downloads_is_refused(self):
        self.assertIn("Applications", self.at("/private/var/folders/x/AppTranslocation/ABC/d/Reclaim.app"))
        self.assertIn("Downloads", self.at("/Users/x/Downloads/Reclaim.app"))

    def test_applications_is_fine(self):
        self.assertIsNone(self.at("/Applications/Reclaim.app"))
        self.assertIsNone(self.at("/Users/x/Applications/Reclaim.app"))
