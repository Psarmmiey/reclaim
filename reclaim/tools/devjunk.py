"""Developer Junk: Xcode, simulators, Homebrew, Docker."""
import datetime
import json
import os
import re
import shutil
import subprocess
import time

from .. import paths
from ..scan import dir_size
from . import Tool, register, remove_items

DEV = os.path.join(paths.HOME, "Library", "Developer")


def _run(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def _which(name):
    for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
        p = os.path.join(d, name)
        if os.access(p, os.X_OK):
            return p
    return shutil.which(name)


def _dir_item(p, label, safety="safe", selected=True, detail=None):
    if not os.path.isdir(p):
        return None
    size = dir_size(p)[0]
    if size < 1024 * 1024:
        return None
    return {"id": p, "path": p, "label": label, "kind": "path", "safety": safety,
            "bytes": size, "selected": selected,
            "detail": detail or p.replace(paths.HOME, "~")}


def _cmd_item(cid, label, cmd, nbytes=None, selected=True, detail="", safety="safe"):
    return {"id": cid, "label": label, "kind": "command", "cmd": cmd,
            "bytes": nbytes, "selected": selected, "safety": safety,
            "detail": detail or " ".join([os.path.basename(cmd[0])] + cmd[1:])}


def _parse_size(s):
    m = re.match(r"([\d.]+)\s*([kMGT]?B)", s or "")
    if not m:
        return 0
    mult = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}
    return int(float(m.group(1)) * mult.get(m.group(2), 1))


def xcode_items(progress):
    out = []
    progress("Xcode build data...")
    for rel, label, safety, sel in [
            ("Xcode/DerivedData", "Derived data (build output and indexes)", "safe", True),
            ("CoreSimulator/Caches", "Simulator caches", "safe", True),
            ("Xcode/Archives", "Archived builds", "review", False)]:
        it = _dir_item(os.path.join(DEV, rel), label, safety, sel)
        if it:
            out.append(it)
    it = _dir_item(os.path.join(paths.HOME, "Library", "Caches", "com.apple.dt.Xcode"),
                   "Xcode cache")
    if it:
        out.append(it)
    for platform in ("iOS", "watchOS", "tvOS", "visionOS"):
        base = os.path.join(DEV, "Xcode", "%s DeviceSupport" % platform)
        try:
            vers = sorted(os.listdir(base))
        except OSError:
            continue
        for i, v in enumerate(vers):
            # Keep the newest symbols; older ones are for devices long updated.
            it = _dir_item(os.path.join(base, v), "%s device support %s" % (platform, v),
                           "review", selected=i < len(vers) - 1)
            if it:
                out.append(it)
    return out


def simulator_items(progress):
    xcrun = _which("xcrun")
    if not xcrun:
        return []
    progress("Simulator runtimes...")
    out = []
    code, so, _ = _run([xcrun, "simctl", "runtime", "list", "-j"], 60)
    if code == 0:
        try:
            runtimes = json.loads(so or "{}")
        except ValueError:
            runtimes = {}
        cutoff = time.time() - 90 * 86400
        for rid, r in runtimes.items():
            if r.get("deletable") is False:
                continue
            used = r.get("lastUsedAt")
            try:
                used_ts = datetime.datetime.fromisoformat(used.replace("Z", "+00:00")).timestamp() if used else 0
            except ValueError:
                used_ts = 0
            name = "%s %s" % (r.get("platformIdentifier", "").split(".")[-1].replace("simulator", " Simulator"),
                              r.get("version", ""))
            out.append(_cmd_item(
                "simrt:" + rid, "Runtime: " + name.strip(),
                [xcrun, "simctl", "runtime", "delete", rid], r.get("sizeBytes"),
                selected=used_ts < cutoff, safety="review",
                detail="last used %s" % (used[:10] if used else "never")))
    code, so, _ = _run([xcrun, "simctl", "list", "devices", "unavailable", "-j"], 60)
    try:
        n = sum(len(v) for v in json.loads(so or "{}").get("devices", {}).values())
    except ValueError:
        n = 0
    if code == 0 and n:
        out.append(_cmd_item("sim:unavailable", "%d simulators for runtimes you no longer have" % n,
                             [xcrun, "simctl", "delete", "unavailable"],
                             detail="xcrun simctl delete unavailable"))
    return out


def brew_items(progress):
    brew = _which("brew")
    if not brew:
        return []
    progress("Homebrew...")
    out = []
    code, so, _ = _run([brew, "--cache"], 30)
    if code == 0 and so.strip():
        it = _dir_item(so.strip(), "Homebrew download cache")
        if it:
            out.append(it)
    out.append(_cmd_item("brew:cleanup", "Old versions of installed formulae",
                         [brew, "cleanup", "--prune=all"],
                         detail="brew cleanup --prune=all"))
    return out


def docker_items(progress):
    docker = _which("docker")
    if not docker:
        return []
    progress("Docker...")
    code, so, _ = _run([docker, "system", "df", "--format", "{{json .}}"], 20)
    if code != 0:
        return [{"id": "docker:off", "label": "Docker is not running", "kind": "command",
                 "disabled": True, "note": "Start Docker Desktop to see what it can free",
                 "bytes": None, "selected": False}]
    rows = {}
    for line in so.splitlines():
        try:
            r = json.loads(line)
            rows[r.get("Type", "")] = _parse_size(r.get("Reclaimable", ""))
        except ValueError:
            continue
    out = []
    for typ, cid, label, cmd, sel, safety in [
            ("Images", "docker:images", "Unused images", ["image", "prune", "-a", "-f"], True, "safe"),
            ("Build Cache", "docker:build", "Build cache", ["builder", "prune", "-a", "-f"], True, "safe"),
            ("Containers", "docker:containers", "Stopped containers", ["container", "prune", "-f"], True, "review"),
            ("Local Volumes", "docker:volumes", "Unused volumes (may hold database data)",
             ["volume", "prune", "-a", "-f"], False, "review")]:
        if rows.get(typ):
            out.append(_cmd_item(cid, label, [docker] + cmd, rows[typ], sel,
                                 "docker " + " ".join(cmd), safety))
    return out


@register
class DevJunk(Tool):
    id = "devjunk"
    name = "Developer Junk"
    blurb = "Xcode build data, old simulator runtimes, Homebrew leftovers and Docker images."
    group = "Cleanup"
    icon = "hammer"

    def scan(self, ctx, progress):
        out = []
        for label, fn in [("Xcode", xcode_items), ("Simulators", simulator_items),
                          ("Homebrew", brew_items), ("Docker", docker_items)]:
            kids = fn(progress)
            if kids:
                out.append({"id": "dev:" + label, "label": label, "children": kids})
        return out

    def act(self, ctx, items, progress):
        return act_mixed(ctx, items, progress)


def act_mixed(ctx, items, progress):
    """Remove path items, run command items."""
    res = remove_items(ctx, [i for i in items if i.get("kind") != "command"], progress)
    for it in items:
        if it.get("kind") != "command":
            continue
        progress("Running %s" % it["detail"])
        code, so, se = _run(it["cmd"], 1800)
        if code == 0:
            res["done"].append(it["id"])
            res["freed_bytes"] += it.get("bytes") or 0
        else:
            res["failed"].append({"id": it["id"], "label": it["label"],
                                  "error": ((se or so).strip().splitlines() or ["failed"])[-1]})
    return res
