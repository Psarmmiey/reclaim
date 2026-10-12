"""Login and Background Items: list them, turn them off, turn them back on."""
import json
import os
import plistlib
import shutil
import subprocess

from .. import paths
from . import Tool, register

USER_AGENTS = os.path.join(paths.HOME, "Library", "LaunchAgents")
OFF_DIR = os.path.join(paths.DATA_DIR, "disabled-agents")
OFF_LOGIN = os.path.join(OFF_DIR, "login-items.json")
SYSTEM_DIRS = [("/Library/LaunchAgents", "For all users"),
               ("/Library/LaunchDaemons", "System services")]
OURS = ("com.reclaim.",)


def _read(p):
    try:
        with open(p, "rb") as f:
            return plistlib.load(f)
    except Exception:
        return {}


def _program(info):
    prog = info.get("Program") or (info.get("ProgramArguments") or [""])[0]
    return prog or ""


def _owner(prog):
    if ".app/" in prog:
        return os.path.basename(prog.split(".app/")[0]) + ".app"
    return os.path.basename(prog) if prog else ""


def _loaded(label):
    try:
        r = subprocess.run(["launchctl", "print", "gui/%d/%s" % (os.getuid(), label)],
                           capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _agent_item(p, on, editable=True, note=""):
    info = _read(p)
    label = info.get("Label") or os.path.basename(p)[:-6]
    prog = _program(info)
    missing = bool(prog) and prog.startswith("/") and not os.path.exists(prog)
    detail = (_owner(prog) or prog or "no program") + (" · program is missing" if missing else "")
    if on and editable:
        detail += " · running" if _loaded(label) else " · not loaded"
    return {"id": "agent:" + p, "path": p, "label": label, "kind": "agent",
            "detail": detail, "bytes": None, "on": on,
            "selected": on and missing and editable,
            "disabled": not editable or label.startswith(OURS),
            "note": note or ("Broken: its program no longer exists" if missing else "")}


def _osa(script):
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def login_items():
    """[(name, path)] from System Events, or (None, error)."""
    js = ('JSON.stringify(Application("System Events").loginItems()'
          '.map(function(i){return [i.name(), i.path()]}))')
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", "-e", js],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    if r.returncode != 0:
        return None, r.stderr.strip()
    try:
        return [tuple(x) for x in json.loads(r.stdout or "[]")], ""
    except ValueError:
        return None, "unreadable answer from System Events"


def _q(s):
    return '"%s"' % str(s).replace("\\", "\\\\").replace('"', '\\"')


def _off_logins():
    try:
        with open(OFF_LOGIN) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save_off_logins(rows):
    os.makedirs(OFF_DIR, exist_ok=True)
    with open(OFF_LOGIN, "w") as f:
        json.dump(rows, f, indent=1)


@register
class Background(Tool):
    id = "background"
    name = "Login and Background Items"
    blurb = "Everything that starts by itself. Turn things off and back on again."
    group = "Performance"
    verb = "Switch"
    icon = "power"

    def scan(self, ctx, progress):
        out = []
        progress("Login items...")
        rows, err = login_items()
        kids = []
        for name, path in rows or []:
            kids.append({"id": "login:" + path, "label": name, "kind": "login", "path": path,
                         "detail": path.replace(paths.HOME, "~"), "bytes": None,
                         "on": True, "selected": False})
        for row in _off_logins():
            kids.append({"id": "login:" + row["path"], "label": row["name"], "kind": "login",
                         "path": row["path"], "detail": "turned off by Reclaim",
                         "bytes": None, "on": False, "selected": False})
        if rows is None and not kids:
            kids.append({"id": "login:none", "label": "Could not read login items",
                         "kind": "login", "disabled": True, "bytes": None,
                         "note": "Allow Reclaim to control System Events: %s" % err[:80]})
        if kids:
            out.append({"id": "bg:login", "label": "Open at login", "children": kids})
        progress("Your background agents...")
        mine = []
        try:
            mine = [_agent_item(os.path.join(USER_AGENTS, n), True)
                    for n in sorted(os.listdir(USER_AGENTS)) if n.endswith(".plist")]
        except OSError:
            pass
        try:
            mine += [_agent_item(os.path.join(OFF_DIR, n), False, note="Turned off by Reclaim")
                     for n in sorted(os.listdir(OFF_DIR)) if n.endswith(".plist")]
        except OSError:
            pass
        if mine:
            out.append({"id": "bg:user", "label": "Your background agents", "children": mine})
        for d, label in SYSTEM_DIRS:
            try:
                kids = [_agent_item(os.path.join(d, n), True, editable=False,
                                    note="Installed for every user; remove with the app's own uninstaller")
                        for n in sorted(os.listdir(d)) if n.endswith(".plist")]
            except OSError:
                kids = []
            if kids:
                out.append({"id": "bg:" + d, "label": label, "children": kids})
        return out

    def act(self, ctx, items, progress):
        uid = os.getuid()
        res = {"freed_bytes": 0, "trashed_bytes": 0, "done": [], "failed": []}
        off = _off_logins()
        for it in items:
            progress("%s %s" % ("Turning off" if it.get("on") else "Turning on", it["label"]))
            try:
                if it["kind"] == "agent":
                    name = os.path.basename(it["path"])
                    if it["on"]:
                        if os.path.dirname(it["path"]) != USER_AGENTS:
                            raise OSError("not one of your agents")
                        subprocess.run(["launchctl", "bootout", "gui/%d" % uid, it["path"]],
                                       capture_output=True)
                        os.makedirs(OFF_DIR, exist_ok=True)
                        shutil.move(it["path"], os.path.join(OFF_DIR, name))
                    else:
                        dest = os.path.join(USER_AGENTS, name)
                        if os.path.exists(dest):
                            raise OSError("an agent with that name is back already")
                        shutil.move(it["path"], dest)
                        subprocess.run(["launchctl", "bootstrap", "gui/%d" % uid, dest],
                                       capture_output=True)
                elif it["kind"] == "login":
                    if it["on"]:
                        code, _, err = _osa('tell application "System Events" to delete '
                                            'login item %s' % _q(it["label"]))
                        if code:
                            raise OSError(err or "System Events refused")
                        off.append({"name": it["label"], "path": it["path"]})
                    else:
                        code, _, err = _osa(
                            'tell application "System Events" to make login item at end '
                            'with properties {path:%s, hidden:false}' % _q(it["path"]))
                        if code:
                            raise OSError(err or "System Events refused")
                        off = [r for r in off if r["path"] != it["path"]]
                    _save_off_logins(off)
                res["done"].append(it["id"])
            except (OSError, subprocess.SubprocessError) as e:
                res["failed"].append({"id": it["id"], "label": it["label"], "error": str(e)})
        return res
