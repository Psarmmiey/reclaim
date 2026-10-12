"""Configuration: a small JSON file with sane defaults."""
import json
import os
import secrets

from . import paths

DEFAULTS = {
    "scan_root": paths.HOME,
    "scan_interval_hours": 12,
    "scan_on_battery": False,              # scheduled scans wait for the charger
    "check_interval_minutes": 30,

    # Free space thresholds, in GB.
    "alert_below_gb": 25,
    "critical_below_gb": 10,
    "auto_clean_below_gb": 15,

    # Auto-clean only ever touches rule-verified caches. AI verdicts never
    # authorize a deletion on their own.
    "auto_clean": True,
    "auto_clean_min_reclaim_gb": 1.0,
    "auto_clean_quiet_hours": [],          # e.g. [0,1,2,3,4,5,6] to skip night

    # Deleted items are moved here first and purged after N days. Set 0 to
    # delete outright.
    "undo_days": 7,
    # Whether a clean keeps an undo copy in Reclaim's trash. The trash is on
    # the same disk, so trashed items free nothing until they are purged.
    #   auto   - delete rule-verified caches outright when space is low,
    #            trash everything else
    #   ask    - the menu bar asks each time (background cleans use auto)
    #   always - always trash
    #   never  - always delete outright
    "trash_policy": "auto",

    "classifier": "claude",                # claude | rules
    "claude_command": "claude",
    "claude_timeout_seconds": 120,

    "server_host": "127.0.0.1",
    "server_port": 8765,
    "server_token": "",                    # generated on first run

    # Clipboard history (menu bar app only).
    "clipboard": True,
    "clipboard_paused": False,
    "clipboard_hotkey": "ctrl+cmd+v",
    "clipboard_paste": True,               # paste into the app you were in
    "clipboard_days": 30,
    "clipboard_max_items": 500,
    "clipboard_ignore_apps": ["1Password", "Bitwarden", "Keychain Access",
                              "Passwords", "LastPass", "Dashlane", "KeePassXC",
                              "Enpass"],

    "exclude": [],
    "min_target_mb": 200,                  # ignore anything smaller
    "notify": True,

    # Updates from GitHub releases: "ask" (tell me), "auto" (install when
    # found), or "off".
    "auto_update": "ask",
}


def load():
    paths.ensure_dirs()
    cfg = dict(DEFAULTS)
    if os.path.exists(paths.CONFIG_FILE):
        try:
            with open(paths.CONFIG_FILE) as f:
                cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
    if not cfg.get("server_token"):
        cfg["server_token"] = secrets.token_urlsafe(24)
        save(cfg)
    return cfg


def save(cfg):
    paths.ensure_dirs()
    tmp = paths.CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
    os.replace(tmp, paths.CONFIG_FILE)
    return cfg


def set_values(**kv):
    cfg = load()
    cfg.update(kv)
    return save(cfg)
