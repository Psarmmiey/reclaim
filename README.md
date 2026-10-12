# Reclaim

Keeps an eye on what is eating your Mac's disk, tells you before it becomes a
problem, and takes the space back when you say so.

- **Menu bar**: free space at a glance, the biggest offenders, one-click clean.
- **Background watcher**: scans on a schedule, notifies you when space runs low.
- **Web dashboard**: sortable table of everything it found, with an audit log.
- **Claude for the unknowns**: anything the rules cannot name gets described by
  your local `claude` CLI, in plain language.
- **Auto-clean, narrowly scoped**: when free space drops below your threshold,
  Reclaim clears caches that rebuild themselves. Nothing else, ever.
- **Tools**: Uninstaller, App Leftovers, AI Clutter, Large and Old Files,
  Duplicate Finder, Downloads, Trash Bins, Developer Junk, and Login and
  Background Items. Each scans, shows what it found, and acts only on what you tick.
- **Clipboard history**: searchable, with pinned items, a picker on
  `ctrl+cmd+v`, and paste straight into the app you were in. Anything a
  password manager marks as concealed is never saved.

## Download

Get the latest build from the [download page](https://psarmmiey.github.io/reclaim/)
or the [releases](https://github.com/psarmmiey/reclaim/releases). It is built for
Apple Silicon and is not signed with a paid Apple certificate, so the first time
you open it you need to allow it under System Settings → Privacy & Security →
Open Anyway. The download page walks through it.

## Updates

Reclaim checks GitHub for a new release once a day. By default it tells you
(menu bar: **Update to x.y.z...**, or a button in the dashboard); set
**Settings → Updates** to install automatically, or turn checks off. An update
is verified against the release's SHA-256, swapped in with the old version kept
as `Reclaim.app.previous`, and restarted. If you have the local signing
certificate from `./build-app.sh`, the update is re-signed with it so macOS
permissions carry over. From the terminal: `reclaim update` (or `--check`).

## Build from source

```sh
./build-app.sh              # build, install to ~/Applications, restart, verify
./build-app.sh --rollback   # back to the previous build
reclaim version             # which build is installed and which is running
```

That builds `Reclaim.app` into `~/Applications`, swaps it in (keeping the previous
build for `--rollback`), restarts it, and checks the new build is the one running.
The first build creates a local signing certificate so macOS permissions such as
Accessibility survive rebuilds. `reclaim` on the command line is a thin wrapper
around the app. Running as a real
app means macOS asks permission for Reclaim itself, not for every Python script.
Grant Accessibility when asked if you want picked clipboard items pasted for you.

For a plain Python install without the app bundle, `./install.sh` still works.
To do it by hand:

```sh
pip install --user '.[menubar]'
reclaim scan
reclaim install          # start at login
```

## Use

```sh
reclaim status                  # free space and current findings
reclaim scan                    # scan now
reclaim clean                   # dry run: what would be removed
reclaim clean --apply           # remove the safe items
reclaim clean --apply --include-review   # also the ones marked review
reclaim restore --list          # things removed recently
reclaim restore --id 7          # put one back
reclaim dashboard               # open the web UI
reclaim app                     # menu bar + watcher in the foreground
reclaim config --set alert_below_gb=40
reclaim uninstall               # stop starting at login
```

## The three safety levels

| Level | Meaning | Auto-clean | Manual delete |
|---|---|---|---|
| **safe** | Regenerates itself: package caches, build output, simulator caches | Yes | Yes |
| **review** | Recoverable but you should look: emulators, Docker disks, big unknown files | Never | Yes |
| **keep** | Personal data and credentials | Never | Never |

Auto-clean only ever touches `safe` items that came from the **built-in rule
list** in `rules.py`. A Claude verdict can move an item to `review` or `keep`,
never to `safe`, so the model can make Reclaim more cautious but never less.
There is a unit test for exactly that.

`Documents`, `Desktop`, `Pictures`, `Movies`, `Music`, iCloud Drive, Mail,
Messages, `.ssh`, `.gnupg`, `.aws` and your keychains are protected: Reclaim
reports what is in them and refuses to delete from them.

## Undo

Deleted items move to `~/.local/share/reclaim/trash` first and are purged after
`undo_days` (7 by default). Until then, `reclaim restore --id N` or the Restore
button in the dashboard puts them back. Set `undo_days=0` to delete outright.

## What Claude sees

Only the path, the size, and up to six child names of folders already marked
`review`. It never reads file contents. Set `classifier` to `rules` in settings
to turn it off entirely.

## The dashboard

Served on `127.0.0.1:8765`, loopback only, and every request needs the token in
`~/.config/reclaim/config.json`. `reclaim status` prints the full URL.

## Layout

```
reclaim/
  rules.py       what each folder is and how safe it is  <- edit this first
  scan.py        the walker that turns a disk into targets
  classify.py    the Claude pass, and the clamp that keeps it honest
  clean.py       guarded deletion, trash, restore
  daemon.py      the schedule, the thresholds, the alerts
  server.py      localhost API
  web/           the dashboard
  menubar.py     rumps app (macOS)
  launchd.py     the LaunchAgent
```

Adding a cache you use is a one-line change to `HOME_CACHES` or `DIR_PATTERNS`
in `rules.py`.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The suite covers the deletion guards (root, `$HOME`, outside home, symlinks,
protected folders, Reclaim's own data) and the classifier clamp.

## Releasing

Releases are cut by [Release Please](https://github.com/googleapis/release-please)
from the commit history, so commit messages follow
[Conventional Commits](https://www.conventionalcommits.org/):

- `fix:` makes a patch release, `feat:` a minor one.
- `docs:`, `test:`, `ci:` and `chore:` are left out of the changelog.
- A `!` after the type (or a `BREAKING CHANGE:` footer) makes a **major** release.
  Use it only when that is the intent.

Each push to `main` updates an open release pull request with the next version
and changelog. Merging it tags the release, and the workflow builds `Reclaim.app`
and attaches it. The download page shows the new version on its own.
