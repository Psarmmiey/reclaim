"""reclaim - the command line face of the same engine the menu bar uses."""
import argparse
import os
import sys
import time
import webbrowser

from . import (__version__, classify, clean, config, format as fmt, launchd,
               paths, rules, scan, server, store)


def c(code, s):
    return "\033[%sm%s\033[0m" % (code, s) if sys.stdout.isatty() else str(s)


B = lambda s: c("1", s)
D = lambda s: c("2", s)
G = lambda s: c("32", s)
Y = lambda s: c("33", s)
R = lambda s: c("31", s)

_TAGCOLOR = {rules.SAFE: G, rules.REVIEW: Y, rules.KEEP: D}


def tag(safety, width=7):
    return _TAGCOLOR[safety](safety.ljust(width))


def _progress(dirs, files, total):
    if sys.stderr.isatty():
        sys.stderr.write("\r  scanning... %d folders, %s files, %s   "
                         % (dirs, "{:,}".format(files), fmt.human(total)))
        sys.stderr.flush()


def _clear():
    if sys.stderr.isatty():
        sys.stderr.write("\r" + " " * 64 + "\r")


# ---------------------------------------------------------------- commands

def cmd_scan(args):
    cfg = config.load()
    con = store.connect()
    if args.root:
        cfg["scan_root"] = os.path.expanduser(args.root)
    print()
    print("  " + B("Scanning %s" % cfg["scan_root"]))
    t0 = time.time()
    scan_id, targets = scan.run_scan(con, cfg, _progress if not args.quiet else None)
    _clear()
    if cfg.get("classifier") == "claude" and not args.no_ai:
        if classify.claude_available(cfg.get("claude_command", "claude")):
            print(D("  asking Claude about the ambiguous ones..."))
            n = classify.classify(con, scan_id, targets, cfg)
            if n:
                print(D("  %d item(s) classified" % n))
        else:
            print(D("  claude CLI not found, using rules only"))
    print(D("  done in %.1fs" % (time.time() - t0)))
    _print_summary(con, targets)
    return 0


def _print_summary(con, targets):
    s = store.latest_scan(con)
    free, total = scan.disk_usage()
    print()
    print("  %s free of %s   %s scanned, %s files"
          % (B(fmt.human(free)), fmt.human(total),
             fmt.human(s["total_bytes"]), "{:,}".format(s["file_count"])))

    by_safety = {}
    for t in targets:
        by_safety.setdefault(t["safety"], []).append(t)
    safe_bytes = sum(t["bytes"] for t in clean.auto_cleanable(targets))
    print("  %s reclaimable without asking" % G(fmt.human(safe_bytes)))
    print()
    print("  %10s  %-8s %-26s %s" % ("SIZE", "SAFETY", "WHAT", "PATH"))
    print("  " + "-" * 76)
    for t in targets[:args_top()]:
        rel = t["path"].replace(paths.HOME, "~")
        print("  %10s  %s  %-26s %s"
              % (fmt.human(t["bytes"]), tag(t["safety"]), t["label"][:26], D(rel)))
        if t.get("reason"):
            print("  %10s  %s" % ("", D("^ " + t["reason"])))
    print()


_TOP = [25]


def args_top():
    return _TOP[0]


def cmd_status(args):
    con = store.connect()
    s = store.latest_scan(con)
    free, total = scan.disk_usage()
    cfg = config.load()
    print()
    print("  " + B("Reclaim %s" % __version__))
    print("  %s free of %s" % (B(fmt.human(free)), fmt.human(total)))
    if not s:
        print("  " + D("no scan yet - run: reclaim scan"))
        print()
        return 0
    targets = store.targets_for(con, s["id"])
    safe = clean.auto_cleanable(targets)
    print("  last scan %s, %d items flagged"
          % (fmt.ago(s["finished_at"]), len(targets)))
    print("  %s reclaimable without asking (%d items)"
          % (G(fmt.human(sum(t["bytes"] for t in safe))), len(safe)))
    print("  %s freed all time" % fmt.human(store.reclaimed_total(con)))
    print("  agent %s" % (G("installed") if launchd.is_installed() else D("not installed")))
    print("  dashboard %s" % D(server.url(cfg)))
    print()
    for t in targets[:10]:
        print("  %10s  %s  %s" % (fmt.human(t["bytes"]), tag(t["safety"]),
                                  D(t["path"].replace(paths.HOME, "~"))))
    print()
    return 0


def cmd_clean(args):
    cfg = config.load()
    con = store.connect()
    s = store.latest_scan(con)
    if not s:
        print("No scan yet. Run: reclaim scan")
        return 1
    targets = store.targets_for(con, s["id"])
    items = clean.auto_cleanable(targets)
    if args.include_review:
        items = [t for t in targets if t["safety"] != rules.KEEP]
    if not items:
        print("\n  Nothing to clean.\n")
        return 0

    total = sum(t["bytes"] for t in items)
    print()
    for t in items:
        print("  %10s  %s  %s" % (fmt.human(t["bytes"]), tag(t["safety"]),
                                  D(t["path"].replace(paths.HOME, "~"))))
    print()
    print("  " + B("%s across %d items" % (fmt.human(total), len(items))))

    if not args.apply:
        print("  " + D("Dry run. Add --apply to remove these."))
        print()
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print(R("  Not a terminal. Use --yes to confirm."))
            return 1
        if input("\n  Remove them? [y/N] ").strip().lower() not in ("y", "yes"):
            print("  Cancelled.\n")
            return 0

    if args.skip_trash:
        trash = False
    elif args.keep_undo:
        trash = True
    else:
        free, _ = scan.disk_usage(cfg.get("scan_root", "~"))
        policy = cfg.get("trash_policy", "auto")
        if policy == "ask":
            policy = "auto"
            if sys.stdin.isatty() and not args.yes and cfg.get("undo_days", 7):
                a = input("  Skip the trash and free the space now? [y/N] ")
                policy = "never" if a.strip().lower() in ("y", "yes") else "always"
        trash = lambda it: clean.should_trash(it, cfg, free, policy)  # noqa: E731
    res = clean.remove_many(con, items, mode="manual",
                            undo_days=cfg.get("undo_days", 7), trash=trash)
    print()
    print("  " + B("Freed %s" % fmt.human(res["freed_bytes"])))
    if res["trashed_bytes"]:
        print("  " + D("%s moved to Reclaim trash; empty it with: "
                       "reclaim restore --empty" % fmt.human(res["trashed_bytes"])))
    if res["failed"]:
        for f in res["failed"]:
            print("  %s %s: %s" % (R("failed"), f["path"], f["error"]))
    if res["trashed_bytes"]:
        print("  " + D("Restore within %d days: reclaim restore --list"
                       % cfg["undo_days"]))
    print()
    return 0


def cmd_restore(args):
    con = store.connect()
    if args.empty:
        from .daemon import Daemon
        print("\n  Emptying Reclaim trash...", flush=True)
        n = Daemon(config.load(), con).empty_trash()
        print("  " + G("Freed %s" % fmt.human(n)) + "\n")
        return 0
    if args.list or not args.id:
        rows = store.restorable(con)
        print()
        if not rows:
            print("  Nothing in Reclaim's trash.\n")
            return 0
        for r in rows:
            print("  %4d  %10s  %s  %s" % (r["id"], fmt.human(r["bytes"]),
                                           fmt.ago(r["at"]),
                                           D(r["path"].replace(paths.HOME, "~"))))
        print()
        print("  " + D("Restore one with: reclaim restore --id N"))
        print()
        return 0
    ok, msg = clean.restore(con, args.id)
    print(("  " + G(msg)) if ok else ("  " + R(msg)))
    return 0 if ok else 1


def cmd_dashboard(args):
    from .daemon import Daemon
    cfg = config.load()
    d = Daemon(cfg)
    httpd = server.build(d, cfg)
    link = server.url(cfg)
    print()
    print("  " + B("Reclaim dashboard"))
    print("  " + link)
    print("  " + D("Ctrl-C to stop"))
    print()
    if not args.no_open:
        webbrowser.open(link)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped\n")
    return 0


def cmd_daemon(args):
    from .daemon import Daemon
    if not paths.single_instance("watcher"):
        print("Reclaim is already running.")
        return 0
    cfg = config.load()
    d = Daemon(cfg)
    server.serve_in_thread(d, cfg)
    print("Reclaim daemon running. Dashboard: %s" % server.url(cfg), flush=True)
    try:
        d.run_forever()
    except KeyboardInterrupt:
        d.stop()
    return 0


def cmd_app(args):
    from . import menubar
    from .daemon import Daemon
    if not paths.single_instance("watcher"):
        print("Reclaim is already running.")
        return 0
    menubar.run(Daemon(config.load()))
    return 0


def _running():
    """The running app's /api/status, or None."""
    import json
    import urllib.request
    cfg = config.load()
    req = urllib.request.Request(
        "http://%s:%s/api/status" % (cfg.get("server_host", "127.0.0.1"), cfg.get("server_port", 8765)),
        headers={"X-Reclaim-Token": cfg["server_token"]})
    try:
        return json.load(urllib.request.urlopen(req, timeout=2))
    except Exception:
        return None


def cmd_version(args):
    from . import version_string
    print("  This copy:   Reclaim %s" % version_string())
    s = _running()
    if s:
        print("  Running now: Reclaim %s (build %s), pid %s" % (s.get("version"), s.get("build"), s.get("pid")))
    else:
        print("  Running now: nothing answering")
    return 0


def cmd_restart(args):
    """Restart the login agent and prove it really came back."""
    import subprocess
    before = (_running() or {}).get("pid")
    target = "gui/%d/%s" % (os.getuid(), paths.PLIST_LABEL)
    r = subprocess.run(["launchctl", "kickstart", "-k", target], capture_output=True, text=True)
    if r.returncode != 0:
        print(R("  launchctl refused: %s" % (r.stderr.strip() or r.returncode)))
        return 1
    for _ in range(40):
        time.sleep(0.5)
        s = _running()
        if s and s.get("pid") != before:
            print("  " + G("Restarted") + ": Reclaim %s (build %s), pid %s"
                  % (s.get("version"), s.get("build"), s.get("pid")))
            return 0
    print(R("  Reclaim did not come back. See ~/.local/share/reclaim/reclaim.log"))
    return 1


def cmd_install(args):
    ok, msg = launchd.install(mode=args.mode)
    if ok:
        print("\n  " + G("Installed") + " %s" % msg)
        print("  Reclaim now starts when you log in.")
        print("  " + D("Dashboard: %s" % server.url(config.load())))
        print()
        return 0
    print("\n  " + R("Could not install: %s" % msg) + "\n")
    return 1


def cmd_uninstall(args):
    ok, msg = launchd.uninstall()
    print("\n  " + (G("Removed") if ok else R(msg)) + "\n")
    return 0


def cmd_config(args):
    cfg = config.load()
    if args.set:
        updates = {}
        for pair in args.set:
            if "=" not in pair:
                print("  expected key=value, got %r" % pair)
                return 1
            k, v = pair.split("=", 1)
            if k not in config.DEFAULTS:
                print("  unknown setting: %s" % k)
                return 1
            cur = config.DEFAULTS[k]
            if isinstance(cur, bool):
                v = v.lower() in ("1", "true", "yes", "on")
            elif isinstance(cur, int):
                v = int(v)
            elif isinstance(cur, float):
                v = float(v)
            updates[k] = v
        cfg = config.set_values(**updates)
    print()
    for k in sorted(cfg):
        if k == "server_token":
            print("  %-26s %s" % (k, D("(hidden)")))
        else:
            print("  %-26s %s" % (k, cfg[k]))
    print()
    print("  " + D(paths.CONFIG_FILE))
    print()
    return 0


# ---------------------------------------------------------------- parser

def build_parser():
    p = argparse.ArgumentParser(
        prog="reclaim",
        description="Watch what is eating your disk, and take it back.")
    p.add_argument("--version", action="version", version="reclaim " + __version__)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("scan", help="scan now and show what was found")
    s.add_argument("root", nargs="?", help="folder to scan (default: config)")
    s.add_argument("--top", type=int, default=25, help="rows to print")
    s.add_argument("--no-ai", action="store_true", help="skip the Claude pass")
    s.add_argument("-q", "--quiet", action="store_true")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("status", help="free space and the current findings")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("clean", help="remove reclaimable items")
    s.add_argument("--apply", action="store_true", help="actually remove them")
    s.add_argument("-y", "--yes", action="store_true", help="skip the prompt")
    s.add_argument("--include-review", action="store_true",
                   help="also remove items marked review (never 'keep')")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--skip-trash", action="store_true",
                   help="delete outright, freeing the space now")
    g.add_argument("--keep-undo", action="store_true",
                   help="move to Reclaim trash even if space is low")
    s.set_defaults(func=cmd_clean)

    s = sub.add_parser("restore", help="put back something Reclaim removed")
    s.add_argument("--id", type=int, help="action id from --list")
    s.add_argument("--list", action="store_true")
    s.add_argument("--empty", action="store_true",
                   help="permanently empty Reclaim trash")
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("dashboard", help="serve the web dashboard")
    s.add_argument("--no-open", action="store_true")
    s.set_defaults(func=cmd_dashboard)

    s = sub.add_parser("daemon", help="run the background watcher (no menu bar)")
    s.set_defaults(func=cmd_daemon)

    s = sub.add_parser("app", help="menu bar app plus daemon (macOS)")
    s.set_defaults(func=cmd_app)

    s = sub.add_parser("install", help="start Reclaim at login")
    s.add_argument("--mode", choices=["app", "daemon"], default="app")
    s.set_defaults(func=cmd_install)

    s = sub.add_parser("version", help="this copy's version, and the running one's")
    s.set_defaults(func=cmd_version)

    s = sub.add_parser("restart", help="restart Reclaim and check it came back")
    s.set_defaults(func=cmd_restart)

    s = sub.add_parser("uninstall", help="stop starting at login")
    s.set_defaults(func=cmd_uninstall)

    s = sub.add_parser("config", help="show or change settings")
    s.add_argument("--set", action="append", metavar="KEY=VALUE")
    s.set_defaults(func=cmd_config)
    return p


def main(argv=None):
    paths.rotate_log()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    if hasattr(args, "top"):
        _TOP[0] = args.top
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nstopped")
        return 130
