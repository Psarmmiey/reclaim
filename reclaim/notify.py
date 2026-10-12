"""macOS notifications, with a graceful nothing-happens fallback elsewhere."""
import shutil
import subprocess
import sys


def _osascript(title, message, subtitle=None, sound=None):
    parts = ['display notification %s' % _q(message),
             'with title %s' % _q(title)]
    if subtitle:
        parts.append('subtitle %s' % _q(subtitle))
    if sound:
        parts.append('sound name %s' % _q(sound))
    script = " ".join(parts)
    subprocess.run(["osascript", "-e", script],
                   capture_output=True, timeout=10, check=False)


def _q(s):
    return '"%s"' % str(s).replace("\\", "\\\\").replace('"', '\\"')


def send(title, message, subtitle=None, urgent=False):
    """Best effort. Never raises."""
    if sys.platform != "darwin":
        return False
    try:
        if shutil.which("terminal-notifier"):
            cmd = ["terminal-notifier", "-title", title, "-message", message]
            if subtitle:
                cmd += ["-subtitle", subtitle]
            if urgent:
                cmd += ["-sound", "Basso"]
            subprocess.run(cmd, capture_output=True, timeout=10, check=False)
            return True
        _osascript(title, message, subtitle, "Basso" if urgent else None)
        return True
    except Exception:
        return False
