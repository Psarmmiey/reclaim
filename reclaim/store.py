"""SQLite state: scans, targets, verdicts and an audit log of every deletion."""
import os
import sqlite3
import threading
import time

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at REAL NOT NULL,
  finished_at REAL,
  root TEXT NOT NULL,
  total_bytes INTEGER DEFAULT 0,
  file_count INTEGER DEFAULT 0,
  dir_count INTEGER DEFAULT 0,
  free_bytes INTEGER DEFAULT 0,
  disk_bytes INTEGER DEFAULT 0,
  errors INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS targets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  kind TEXT NOT NULL,
  label TEXT NOT NULL,
  safety TEXT NOT NULL,
  mtime REAL,
  reason TEXT,
  source TEXT DEFAULT 'rules'
);
CREATE INDEX IF NOT EXISTS idx_targets_scan ON targets(scan_id, bytes DESC);

CREATE TABLE IF NOT EXISTS verdicts (
  path TEXT PRIMARY KEY,
  safety TEXT NOT NULL,
  reason TEXT,
  source TEXT NOT NULL,
  decided_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at REAL NOT NULL,
  path TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  label TEXT,
  mode TEXT NOT NULL,
  outcome TEXT NOT NULL,
  trash_path TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_at ON actions(at DESC);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at REAL NOT NULL,
  level TEXT NOT NULL,
  message TEXT NOT NULL
);
"""


class Database:
    """One SQLite connection per thread.

    The daemon loop, the HTTP server's worker threads and the menu bar all
    touch the database, and a sqlite3 connection may only be used by the
    thread that opened it. WAL mode lets those per-thread connections coexist.
    """

    def __init__(self, path, schema=None):
        self.path = path
        self.schema = schema or SCHEMA
        self._local = threading.local()

    @property
    def raw(self):
        con = getattr(self._local, "con", None)
        if con is None:
            paths.ensure_dirs()
            con = sqlite3.connect(self.path, timeout=15)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA foreign_keys=ON")
            con.executescript(self.schema)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            self._local.con = con
        return con

    def execute(self, *a, **kw):
        return self.raw.execute(*a, **kw)

    def executemany(self, *a, **kw):
        return self.raw.executemany(*a, **kw)

    def commit(self):
        return self.raw.commit()

    def close(self):
        con = getattr(self._local, "con", None)
        if con is not None:
            con.close()
            self._local.con = None


def connect():
    db = Database(paths.DB_FILE)
    db.raw                     # open now so a schema problem surfaces here
    return db


def start_scan(con, root):
    cur = con.execute("INSERT INTO scans (started_at, root) VALUES (?, ?)",
                      (time.time(), root))
    con.commit()
    return cur.lastrowid


def finish_scan(con, scan_id, **fields):
    fields["finished_at"] = time.time()
    cols = ", ".join("%s = ?" % k for k in fields)
    con.execute("UPDATE scans SET %s WHERE id = ?" % cols,
                list(fields.values()) + [scan_id])
    con.commit()


def latest_scan(con):
    row = con.execute("SELECT * FROM scans WHERE finished_at IS NOT NULL "
                      "ORDER BY finished_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def scan_history(con, limit=30):
    rows = con.execute("SELECT * FROM scans WHERE finished_at IS NOT NULL "
                       "ORDER BY finished_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def add_targets(con, scan_id, targets):
    con.executemany(
        "INSERT INTO targets (scan_id, path, bytes, kind, label, safety, mtime, "
        "reason, source) VALUES (?,?,?,?,?,?,?,?,?)",
        [(scan_id, t["path"], t["bytes"], t["kind"], t["label"], t["safety"],
          t.get("mtime"), t.get("reason"), t.get("source", "rules"))
         for t in targets])
    con.commit()


def targets_for(con, scan_id, safety=None, limit=500):
    q = "SELECT * FROM targets WHERE scan_id = ?"
    args = [scan_id]
    if safety:
        q += " AND safety = ?"
        args.append(safety)
    q += " ORDER BY bytes DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in con.execute(q, args).fetchall()]


def update_target_safety(con, scan_id, path, safety, reason, source):
    con.execute("UPDATE targets SET safety=?, reason=?, source=? "
                "WHERE scan_id=? AND path=?",
                (safety, reason, source, scan_id, path))
    con.commit()


def get_verdict(con, path):
    row = con.execute("SELECT * FROM verdicts WHERE path = ?", (path,)).fetchone()
    return dict(row) if row else None


def put_verdict(con, path, safety, reason, source):
    con.execute(
        "INSERT INTO verdicts (path, safety, reason, source, decided_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
        "safety=excluded.safety, reason=excluded.reason, "
        "source=excluded.source, decided_at=excluded.decided_at",
        (path, safety, reason, source, time.time()))
    con.commit()


def log_action(con, path, size, label, mode, outcome, trash_path=None, error=None):
    con.execute(
        "INSERT INTO actions (at, path, bytes, label, mode, outcome, trash_path, "
        "error) VALUES (?,?,?,?,?,?,?,?)",
        (time.time(), path, size, label, mode, outcome, trash_path, error))
    con.commit()


def recent_actions(con, limit=100):
    rows = con.execute("SELECT * FROM actions ORDER BY at DESC LIMIT ?",
                       (limit,)).fetchall()
    return [dict(r) for r in rows]


def reclaimed_total(con, since=0.0):
    row = con.execute(
        "SELECT COALESCE(SUM(bytes),0) AS n FROM actions "
        "WHERE outcome IN ('trashed','deleted') AND at >= ?", (since,)).fetchone()
    return row["n"]


def restorable(con):
    rows = con.execute(
        "SELECT * FROM actions WHERE outcome='trashed' AND trash_path IS NOT NULL "
        "ORDER BY at DESC").fetchall()
    return [dict(r) for r in rows if os.path.exists(r["trash_path"])]


def log_event(con, level, message):
    con.execute("INSERT INTO events (at, level, message) VALUES (?,?,?)",
                (time.time(), level, message))
    con.commit()


def recent_events(con, limit=50):
    rows = con.execute("SELECT * FROM events ORDER BY at DESC LIMIT ?",
                       (limit,)).fetchall()
    return [dict(r) for r in rows]
