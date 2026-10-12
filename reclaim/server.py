"""A small token-authenticated HTTP server bound to localhost.

Serves the dashboard and a JSON API. It never listens on anything but the
loopback interface, and every request must carry the token from your config.
"""
import json
import os
import posixpath
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import clean, clipboard, config, format as fmt, store

_CLIPS = []


def _clips(d):
    """The menu bar's clipboard database, or our own handle on the same file."""
    if d.clipboard is not None:
        return d.clipboard.con
    if not _CLIPS:
        _CLIPS.append(clipboard.connect())
    return _CLIPS[0]

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class Handler(BaseHTTPRequestHandler):
    daemon_ref = None
    token = ""
    server_version = "Reclaim"

    def log_message(self, fmt_str, *args):
        pass                                   # keep stdout clean

    # ------------------------------------------------------------- helpers

    def _authed(self, query):
        supplied = (self.headers.get("X-Reclaim-Token")
                    or query.get("token", [""])[0])
        return bool(self.token) and secrets.compare_digest(supplied, self.token)

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name, ctype):
        path = os.path.join(WEB_DIR, name)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return {}
        if n <= 0 or n > 1 << 20:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode() or "{}")
        except ValueError:
            return {}

    # ------------------------------------------------------------- routing

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = posixpath.normpath(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)

        if route == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if route in ("/", "/index.html"):
            if not self._authed(query):
                self.send_error(401, "Missing or bad token")
                return
            return self._file("dashboard.html", "text/html; charset=utf-8")

        if not route.startswith("/api/"):
            self.send_error(404)
            return
        if not self._authed(query):
            return self._json({"error": "unauthorized"}, 401)

        d = self.daemon_ref
        con = d.con

        if route == "/api/status":
            return self._json(d.status())

        if route == "/api/targets":
            s = store.latest_scan(con)
            targets = store.targets_for(con, s["id"], limit=400) if s else []
            return self._json({"scan": s, "targets": targets})

        if route == "/api/actions":
            return self._json({"actions": store.recent_actions(con, 150),
                               "restorable": store.restorable(con)})

        if route == "/api/clipboard":
            q = (query.get("q") or [""])[0].strip()
            return self._json({"clips": clipboard.recent(_clips(d), 300, q or None),
                               "recording": d.clipboard is not None,
                               "paused": bool(d.cfg.get("clipboard_paused")),
                               "hotkey": d.cfg.get("clipboard_hotkey")})

        if route.startswith("/api/clipboard/") and route.endswith("/image"):
            try:
                c = clipboard.get(_clips(d), int(route.split("/")[3]))
            except ValueError:
                c = None
            if not c or c["kind"] != "image" or not c["data"]:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(c["data"])))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            self.wfile.write(c["data"])
            return

        if route == "/api/tools":
            return self._json({"tools": d.tools.summary()})

        if route.startswith("/api/tools/"):
            det = d.tools.detail(route.split("/")[3])
            return self._json(det) if det else self._json({"error": "no such tool"}, 404)

        if route == "/api/trash":
            days = d.cfg.get("undo_days", 7)
            items = []
            for r in store.restorable(con):
                name = os.path.basename(r["trash_path"])
                at = clean.trashed_at(name, r["at"])
                r["expires_at"] = at + days * 86400
                items.append(r)
            return self._json({"items": items, "undo_days": days,
                               "bytes": sum(r["bytes"] or 0 for r in items),
                               "emptying": d.status()["emptying"]})

        if route == "/api/history":
            return self._json({"scans": store.scan_history(con, 40),
                               "events": store.recent_events(con, 40)})

        if route == "/api/config":
            cfg = dict(d.cfg)
            cfg.pop("server_token", None)
            return self._json(cfg)

        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        route = posixpath.normpath(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        if not self._authed(query):
            return self._json({"error": "unauthorized"}, 401)

        d = self.daemon_ref
        con = d.con
        body = self._body()

        if route == "/api/scan":
            threading.Thread(target=d.scan_now, daemon=True).start()
            return self._json({"started": True})

        if route == "/api/clean":
            wanted = body.get("paths") or []
            if not isinstance(wanted, list) or not wanted:
                return self._json({"error": "no paths given"}, 400)
            s = store.latest_scan(con)
            if not s:
                return self._json({"error": "no scan yet"}, 400)
            known = {t["path"]: t for t in store.targets_for(con, s["id"],
                                                             limit=1000)}
            items, unknown = [], []
            for p in wanted[:500]:
                t = known.get(p)
                if not t:
                    unknown.append(p)
                    continue
                if t["safety"] == "keep":
                    unknown.append(p)
                    continue
                items.append(t)
            trash = body.get("trash")
            result = clean.remove_many(
                con, items, mode="manual", undo_days=d.cfg.get("undo_days", 7),
                trash=d.trash_decider(trash if isinstance(trash, bool) else None))
            d.touch()
            result["rejected"] = unknown
            result["freed_human"] = fmt.human(result["freed_bytes"])
            result["trashed_human"] = fmt.human(result["trashed_bytes"])
            return self._json(result)

        if route == "/api/clean-safe":
            trash = body.get("trash")
            result = d.clean_safe(mode="manual",
                                  trash=trash if isinstance(trash, bool) else None)
            result["freed_human"] = fmt.human(result["freed_bytes"])
            result["trashed_human"] = fmt.human(result["trashed_bytes"])
            return self._json(result)

        if route == "/api/empty-trash":
            return self._json({"started": d.empty_trash_async()})

        if route == "/api/update/check":
            threading.Thread(target=d.updater.check, daemon=True).start()
            return self._json({"started": True})

        if route == "/api/update/install":
            if not d.updater.state["available"]:
                d.updater.check()                 # synchronous; bounded by its timeout
            if not d.updater.state["available"]:
                return self._json({"error": d.updater.state["error"] or "no update available"}, 409)

            def go():
                try:
                    d.updater.install()
                except Exception as e:
                    store.log_event(d.con, "error", "Update failed: %s" % e)
            threading.Thread(target=go, daemon=True).start()
            return self._json({"started": True})

        if route == "/api/clipboard/clear":
            clipboard.clear(_clips(d))
            d.touch()
            return self._json({"ok": True})

        if route.startswith("/api/clipboard/"):
            parts = route.split("/")              # /api/clipboard/<id>/<verb>
            try:
                cid = int(parts[3])
            except (IndexError, ValueError):
                return self._json({"error": "bad id"}, 400)
            verb = parts[4] if len(parts) == 5 else ""
            con = _clips(d)
            if verb == "copy":
                if d.clipboard is None:
                    return self._json({"error": "The menu bar app is not running"}, 409)
                d.clipboard.request_copy(cid)
            elif verb in ("pin", "unpin"):
                clipboard.set_pinned(con, cid, verb == "pin")
            elif verb == "delete":
                clipboard.delete(con, cid)
            else:
                return self._json({"error": "not found"}, 404)
            d.touch()
            return self._json({"ok": True})

        if route.startswith("/api/tools/"):
            parts = route.split("/")              # /api/tools/<id>/<verb>
            if len(parts) != 5 or parts[4] not in ("scan", "act"):
                return self._json({"error": "not found"}, 404)
            if parts[4] == "scan":
                return self._json({"started": d.tools.scan(parts[3])})
            ids = body.get("ids")
            if not isinstance(ids, list):
                return self._json({"error": "ids must be a list"}, 400)
            return self._json({"started": d.tools.act(parts[3], [str(i) for i in ids[:5000]])})

        if route == "/api/restore-all":
            done, failed = 0, []
            for r in store.restorable(con):
                ok, msg = clean.restore(con, r["id"])
                if ok:
                    done += 1
                else:
                    failed.append({"path": r["path"], "error": msg})
            d.touch()
            return self._json({"restored": done, "failed": failed})

        if route == "/api/restore":
            ok, msg = clean.restore(con, int(body.get("id", 0)))
            d.touch()
            return self._json({"ok": ok, "message": msg})

        if route == "/api/config":
            allowed = {"alert_below_gb", "critical_below_gb", "auto_clean",
                       "auto_clean_below_gb", "scan_interval_hours",
                       "check_interval_minutes", "notify", "undo_days",
                       "classifier", "min_target_mb", "scan_root",
                       "trash_policy", "clipboard", "clipboard_paused",
                       "clipboard_hotkey", "clipboard_paste", "clipboard_days",
                       "clipboard_max_items", "auto_update"}
            if body.get("auto_update") not in (None, "ask", "auto", "off"):
                return self._json({"error": "auto_update must be ask, auto or off"}, 400)
            if body.get("trash_policy") not in (None,) + clean.TRASH_POLICIES:
                return self._json({"error": "bad trash_policy"}, 400)
            if "clipboard_hotkey" in body and not clipboard.parse_hotkey(body["clipboard_hotkey"]):
                return self._json({"error": "Use something like ctrl+cmd+v"}, 400)
            updates = {k: v for k, v in body.items() if k in allowed}
            if updates:
                d.cfg = config.set_values(**updates)
                d.touch()
                for fn in list(d.on_config):
                    try:
                        fn()
                    except Exception:
                        pass
            cfg = dict(d.cfg)
            cfg.pop("server_token", None)
            return self._json(cfg)

        return self._json({"error": "not found"}, 404)


def build(daemon, cfg):
    Handler.daemon_ref = daemon
    Handler.token = cfg["server_token"]
    host = cfg.get("server_host", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1"):
        host = "127.0.0.1"                     # never expose this to a network
    port = int(cfg.get("server_port", 8765))
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        raise SystemExit(
            "Port %d is already in use. Another Reclaim is probably running "
            "(check the menu bar), or change it with:\n"
            "  reclaim config --set server_port=8766\n(%s)" % (port, e))
    httpd.daemon_threads = True
    return httpd


def url(cfg):
    return "http://%s:%s/?token=%s" % (
        cfg.get("server_host", "127.0.0.1"),
        cfg.get("server_port", 8765),
        urllib.parse.quote(cfg["server_token"]))


def serve_in_thread(daemon, cfg):
    httpd = build(daemon, cfg)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd
