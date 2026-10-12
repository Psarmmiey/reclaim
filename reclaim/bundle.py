"""Knowing whether we run from Reclaim.app or from a plain Python install."""
import os
import sys

BUNDLE_ID = "com.reclaim.app"


def frozen():
    """True inside the py2app bundle."""
    return getattr(sys, "frozen", None) == "macosx_app"


def app_path():
    """/path/to/Reclaim.app, or None when not bundled."""
    if not frozen():
        return None
    res = os.environ.get("RESOURCEPATH")
    if not res:
        return None
    return os.path.dirname(os.path.dirname(os.path.abspath(res)))


def executable():
    """The bundle's own launcher, so macOS attributes permissions to Reclaim."""
    app = app_path()
    if not app:
        return None
    return os.path.join(app, "Contents", "MacOS", "Reclaim")


def install_problem():
    """Why this copy cannot be the login item yet, or None.

    A downloaded app opened straight from Downloads runs from a randomised,
    read-only copy (App Translocation); a login item pointing there breaks.
    """
    app = app_path()
    if not app:
        return None
    if "/AppTranslocation/" in app:
        return "Move Reclaim to your Applications folder, then open it from there."
    if os.sep + "Downloads" + os.sep in app:
        return "Move Reclaim out of Downloads into Applications first."
    return None


def launch_command(mode):
    """argv that starts Reclaim in `mode`, from wherever we are installed."""
    exe = executable()
    if exe:
        return [exe, mode]
    return [sys.executable, "-m", "reclaim", mode]
