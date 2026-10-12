"""py2app build for Reclaim.app. Run through build-app.sh, not directly."""
from setuptools import setup

from reclaim import BUILD, __version__

setup(
    name="Reclaim",
    app=["reclaim_main.py"],
    options={"py2app": {
        "argv_emulation": False,
        "packages": ["reclaim", "rumps"],
        "includes": ["AppKit", "Foundation", "objc", "PyObjCTools.AppHelper"],
        "plist": {
            "CFBundleName": "Reclaim",
            "CFBundleDisplayName": "Reclaim",
            "CFBundleIdentifier": "com.reclaim.app",
            "CFBundleExecutable": "Reclaim",
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": BUILD,
            "LSUIElement": True,
            "LSMinimumSystemVersion": "12.0",
            "NSHumanReadableCopyright": "Samuel Oladipupo",
            "NSAppleEventsUsageDescription":
                "Reclaim asks Finder to empty the Bin and remove apps that need "
                "your password, and System Events to manage login items.",
        },
    }},
)
