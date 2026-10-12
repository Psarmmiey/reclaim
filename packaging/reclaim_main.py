"""Entry point of Reclaim.app.

Opened from Finder or launchd with no arguments it runs the menu bar app.
Called with arguments (by the `reclaim` shim) it is the normal CLI.
"""
import sys

from reclaim.cli import main

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-psn_")]
    sys.exit(main(args or ["app"]))
