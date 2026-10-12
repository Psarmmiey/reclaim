"""Install Reclaim as a login item via launchd."""
import os
import subprocess
import sys
import time

from xml.sax.saxutils import escape

from . import bundle, paths

TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>ProcessType</key><string>{process_type}</string>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
  <key>WorkingDirectory</key><string>{home}</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>{path}</string></dict>
</dict>
</plist>
"""


def write_plist(mode="app"):
    paths.ensure_dirs()
    os.makedirs(os.path.dirname(paths.PLIST_FILE), exist_ok=True)
    content = TEMPLATE.format(
        label=paths.PLIST_LABEL,
        args="\n".join("    <string>%s</string>" % escape(a)
                       for a in bundle.launch_command(mode)),
        # Background throttles the whole process's CPU and disk, which stalls
        # the menu bar whenever a scan or a purge is busy. Only the headless
        # daemon gets it; heavy deletes run in their own throttled child.
        process_type="Interactive" if mode == "app" else "Background",
        log=paths.LOG_FILE,
        home=paths.HOME,
        path=os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    )
    with open(paths.PLIST_FILE, "w") as f:
        f.write(content)
    return paths.PLIST_FILE


def _launchctl(*args):
    return subprocess.run(["launchctl"] + list(args), capture_output=True,
                          text=True, check=False)


def _loaded(uid):
    return _launchctl("print", "gui/%d/%s" % (uid, paths.PLIST_LABEL)).returncode == 0


def _wait(cond, seconds=10.0):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.25)
    return cond()


def install(mode="app"):
    if sys.platform != "darwin":
        return False, "launchd is macOS only"
    plist = write_plist(mode)
    uid = os.getuid()
    # bootout returns before the old job has gone; bootstrapping too early fails.
    target = "gui/%d/%s" % (uid, paths.PLIST_LABEL)
    _launchctl("bootout", target)
    _wait(lambda: not _loaded(uid))
    # An old `launchctl unload -w` leaves a "disabled" override behind, and
    # bootstrap then fails with error 5 until it is cleared.
    _launchctl("enable", target)
    r = None
    for _ in range(3):
        r = _launchctl("bootstrap", "gui/%d" % uid, plist)
        if _wait(lambda: _loaded(uid), 3):
            return True, plist
        time.sleep(1)
    return False, (r.stderr.strip() or "launchctl did not load the agent")


def uninstall():
    if sys.platform != "darwin":
        return False, "launchd is macOS only"
    uid = os.getuid()
    # bootout only: `unload -w` would mark the agent disabled for good.
    _launchctl("bootout", "gui/%d/%s" % (uid, paths.PLIST_LABEL))
    try:
        os.remove(paths.PLIST_FILE)
    except OSError:
        pass
    return True, "removed"


def is_installed():
    return os.path.exists(paths.PLIST_FILE)
