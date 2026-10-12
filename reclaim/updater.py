"""Updates from GitHub releases: check, download, verify, swap, restart.

The swap mirrors build-app.sh: the current bundle becomes
Reclaim.app.previous (so `./build-app.sh --rollback` still works) and the
new one takes its place. Release builds are ad-hoc signed by CI; if this Mac
has the "Reclaim Local Signing" certificate, the update is re-signed with it
so macOS permissions such as Accessibility carry over.
"""
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import threading
import time
import urllib.request

from . import __version__, bundle, paths

REPO = "Psarmmiey/reclaim"
API = "https://api.github.com/repos/%s/releases/latest" % REPO
ASSET = "Reclaim-macOS.zip"
SIGN_NAME = "Reclaim Local Signing"
CHECK_EVERY = 24 * 3600
UPDATES_DIR = os.path.join(paths.DATA_DIR, "updates")


class UpdateError(Exception):
    pass


def parse_version(v):
    """'v0.10.2' -> (0, 10, 2). Anything unparseable sorts lowest."""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else (-1, -1, -1)


def is_newer(candidate, current):
    return parse_version(candidate) > parse_version(current)


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Reclaim/%s" % __version__,
        "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def latest_release(opener=_get):
    """The newest published release that has a Mac build attached."""
    with opener(API) as r:
        rel = json.load(r)
    assets = {a["name"]: a for a in rel.get("assets") or []}
    if ASSET not in assets or ASSET + ".sha256" not in assets:
        raise UpdateError("the latest release has no Mac build yet")
    return {
        "version": rel["tag_name"].lstrip("v"),
        "tag": rel["tag_name"],
        "url": assets[ASSET]["browser_download_url"],
        "sha_url": assets[ASSET + ".sha256"]["browser_download_url"],
        "size": assets[ASSET].get("size"),
        "notes_url": rel.get("html_url"),
        "published_at": rel.get("published_at"),
    }


def sign_identity():
    """SHA-1 of the local signing certificate, or None."""
    try:
        r = subprocess.run(["security", "find-identity", "-p", "codesigning",
                            os.path.join(paths.HOME, "Library", "Keychains", "login.keychain-db")],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in r.stdout.splitlines():
        if '"%s"' % SIGN_NAME in line:
            parts = line.split()
            if len(parts) > 1 and re.fullmatch(r"[0-9A-F]{40}", parts[1]):
                return parts[1]
    return None


class Updater:
    """Owned by the daemon. All state is plain data the UIs can show."""

    def __init__(self, daemon, opener=_get, runner=subprocess.run):
        self.daemon = daemon
        self.opener = opener
        self.run = runner
        self.state = {"checking": False, "installing": False, "available": None,
                      "error": None, "checked_at": None, "progress": None}
        self._lock = threading.Lock()
        self.notified = set()

    # ------------------------------------------------------------ check

    def due(self):
        t = self.state["checked_at"]
        return t is None or time.time() - t > CHECK_EVERY

    def check(self):
        """Ask GitHub. Returns the release dict if newer, else None."""
        with self._lock:
            if self.state["checking"] or self.state["installing"]:
                return self.state["available"]
            self.state.update(checking=True, error=None)
        try:
            rel = latest_release(self.opener)
            self.state["available"] = rel if is_newer(rel["version"], __version__) else None
        except UpdateError as e:
            self.state.update(available=None, error=str(e))
        except Exception as e:                       # offline, rate-limited...
            self.state["error"] = "could not reach GitHub: %s" % e
        finally:
            self.state.update(checking=False, checked_at=time.time())
            self.daemon.touch()
        return self.state["available"]

    # ---------------------------------------------------------- install

    def install(self):
        """Download, verify and swap in the available update, then restart.

        Runs on the caller's thread; the UIs call it from a worker.
        """
        rel = self.state["available"]
        app = bundle.app_path()
        if not rel:
            raise UpdateError("no update available")
        if not app:
            raise UpdateError("this copy runs from source; update with git pull and ./build-app.sh")
        if not os.access(os.path.dirname(app), os.W_OK):
            raise UpdateError("cannot write to %s" % os.path.dirname(app))
        with self._lock:
            if self.state["installing"]:
                raise UpdateError("an update is already being installed")
            self.state.update(installing=True, error=None, progress="Downloading")
        self.daemon.touch()
        try:
            new_app = self._fetch(rel)
            self._swap(app, new_app)
        except Exception as e:
            self.state.update(installing=False, progress=None, error=str(e))
            self.daemon.touch()
            raise
        self.state["progress"] = "Restarting"
        self.daemon.touch()
        self._restart(app)

    def _fetch(self, rel):
        work = os.path.join(UPDATES_DIR, rel["version"])
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work)
        zpath = os.path.join(work, ASSET)
        h = hashlib.sha256()
        with self.opener(rel["url"], timeout=60) as r, open(zpath, "wb") as f:
            total = rel.get("size") or 0
            done = 0
            for chunk in iter(lambda: r.read(1 << 16), b""):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if total:
                    self.state["progress"] = "Downloading %d%%" % (done * 100 // total)
        with self.opener(rel["sha_url"]) as r:
            want = r.read().decode().split()[0].lower()
        if h.hexdigest() != want:
            raise UpdateError("download is corrupt (checksum mismatch)")
        self.state["progress"] = "Unpacking"
        out = os.path.join(work, "out")
        self._check(["/usr/bin/ditto", "-x", "-k", zpath, out], "unpack")
        new_app = os.path.join(out, "Reclaim.app")
        try:
            with open(os.path.join(new_app, "Contents", "Info.plist"), "rb") as f:
                info = plistlib.load(f)
        except (OSError, ValueError, plistlib.InvalidFileException):
            raise UpdateError("the download does not contain Reclaim.app")
        if info.get("CFBundleIdentifier") != bundle.BUNDLE_ID:
            raise UpdateError("the download is not Reclaim (bundle id %r)"
                              % info.get("CFBundleIdentifier"))
        if info.get("CFBundleShortVersionString") != rel["version"]:
            raise UpdateError("the download is version %s, expected %s"
                              % (info.get("CFBundleShortVersionString"), rel["version"]))
        ident = sign_identity()
        if ident:
            self.state["progress"] = "Signing"
            self._check(["/usr/bin/codesign", "--force", "--deep", "--sign", ident, new_app],
                        "re-sign")
        return new_app

    def _swap(self, app, new_app):
        self.state["progress"] = "Installing"
        prev = app + ".previous"
        shutil.rmtree(prev, ignore_errors=True)
        os.rename(app, prev)
        try:
            shutil.move(new_app, app)
        except OSError:
            os.rename(prev, app)                 # put the old one back
            raise

    def _restart(self, app):
        from . import launchd
        if launchd.is_installed():
            # kickstart -k stops this process and starts the new bundle.
            self.run(["launchctl", "kickstart", "-k",
                      "gui/%d/%s" % (os.getuid(), paths.PLIST_LABEL)], capture_output=True)
        else:
            subprocess.Popen(["/bin/sh", "-c", 'sleep 1; /usr/bin/open "$0"', app],
                             start_new_session=True)
            os._exit(0)

    def _check(self, cmd, what):
        r = self.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise UpdateError("could not %s: %s" % (what, (r.stderr or "").strip()[-200:]))
