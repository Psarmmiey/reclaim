"""Shared formatting so the CLI, menu bar and dashboard all agree."""
import time

GB = 1024 ** 3


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024 or unit == "PB":
            return "%d %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0


def gb(n):
    return (n or 0) / GB


def ago(ts):
    if not ts:
        return "never"
    d = time.time() - ts
    if d < 90:
        return "just now"
    if d < 3600:
        return "%d min ago" % (d / 60)
    if d < 86400:
        return "%d hr ago" % (d / 3600)
    if d < 86400 * 60:
        return "%d days ago" % (d / 86400)
    if d < 86400 * 730:
        return "%d months ago" % (d / 2629800)
    return "%.1f years ago" % (d / 31557600)
