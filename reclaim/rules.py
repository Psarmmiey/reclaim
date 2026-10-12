"""What each kind of folder is, and how safe it is to remove.

safety levels
  safe    - regenerates itself; eligible for automatic cleaning
  review  - recoverable but you should look first; never auto-cleaned
  keep    - personal data; only ever surfaced, never proposed for deletion
"""

SAFE, REVIEW, KEEP = "safe", "review", "keep"

# Exact absolute paths (relative to $HOME) that are pure caches.
HOME_CACHES = [
    (".gradle/caches",                              "Gradle dependency cache", SAFE),
    (".gradle/daemon",                              "Gradle daemon logs", SAFE),
    (".npm/_cacache",                               "npm download cache", SAFE),
    (".yarn/cache",                                 "Yarn cache", SAFE),
    (".pnpm-store",                                 "pnpm store", SAFE),
    (".cache/uv",                                   "uv wheel cache", SAFE),
    (".cache/pip",                                  "pip cache", SAFE),
    (".cache/yarn",                                 "Yarn cache", SAFE),
    (".cache/go-build",                             "Go build cache", SAFE),
    (".cache/puppeteer",                            "Puppeteer browsers", SAFE),
    (".cache/ms-playwright",                        "Playwright browsers", SAFE),
    (".cargo/registry/cache",                       "Cargo registry cache", SAFE),
    (".m2/repository",                              "Maven repository", SAFE),
    (".ivy2/cache",                                 "Ivy cache", SAFE),
    (".pub-cache/hosted",                           "Dart pub cache", SAFE),
    (".cocoapods/repos",                            "CocoaPods specs", REVIEW),
    ("Library/Caches/Homebrew",                     "Homebrew downloads", SAFE),
    ("Library/Caches/CocoaPods",                    "CocoaPods cache", SAFE),
    ("Library/Caches/pip",                          "pip cache", SAFE),
    ("Library/Caches/Yarn",                         "Yarn cache", SAFE),
    ("Library/Caches/typescript",                   "TypeScript cache", SAFE),
    ("Library/Caches/JetBrains",                    "JetBrains caches", SAFE),
    ("Library/Caches/com.apple.dt.Xcode",           "Xcode cache", SAFE),
    ("Library/Caches/Google/Chrome",                "Chrome cache", SAFE),
    ("Library/Developer/Xcode/DerivedData",         "Xcode build intermediates", SAFE),
    ("Library/Developer/CoreSimulator/Caches",      "Simulator caches", SAFE),
    ("Library/Developer/Xcode/iOS DeviceSupport",   "iOS device symbols", REVIEW),
    ("Library/Developer/Xcode/Archives",            "Xcode archives", REVIEW),
    ("Library/Developer/CoreSimulator/Devices",     "Simulator devices", REVIEW),
    ("Library/Android/sdk/system-images",           "Android system images", REVIEW),
    (".android/avd",                                "Android emulators", REVIEW),
    ("Library/Containers/com.docker.docker/Data/vms",
                                                    "Docker virtual disk", REVIEW),
    ("Library/Application Support/Google/Chrome/OptGuideOnDeviceModel",
                                                    "Chrome on-device model", SAFE),
]

# Directory basenames found anywhere under the scan root.
DIR_PATTERNS = [
    ("node_modules",     "npm packages", SAFE),
    ("__pycache__",      "Python bytecode", SAFE),
    (".mypy_cache",      "mypy cache", SAFE),
    (".pytest_cache",    "pytest cache", SAFE),
    (".ruff_cache",      "ruff cache", SAFE),
    (".tox",             "tox environments", SAFE),
    (".venv",            "Python virtualenv", REVIEW),
    ("venv",             "Python virtualenv", REVIEW),
    (".next",            "Next.js build", SAFE),
    (".nuxt",            "Nuxt build", SAFE),
    (".turbo",           "Turborepo cache", SAFE),
    (".parcel-cache",    "Parcel cache", SAFE),
    (".dart_tool",       "Dart build tooling", SAFE),
    (".terraform",       "Terraform providers", SAFE),
    ("DerivedData",      "Xcode build intermediates", SAFE),
    ("Pods",             "CocoaPods", SAFE),
    ("Carthage",         "Carthage", SAFE),
    (".stack-work",      "Haskell build", SAFE),
    ("target",           "Rust/Java build output", REVIEW),
    ("build",            "build output", REVIEW),
    ("dist",             "build output", REVIEW),
    (".gradle",          "project Gradle state", REVIEW),
]

# Single files worth surfacing.
FILE_PATTERNS = [
    ("*.hprof",          "JVM heap dump", SAFE),
    ("*.crash",          "crash report", SAFE),
    ("*.dmp",            "crash dump", SAFE),
    ("core.*",           "core dump", SAFE),
]

# Scanned and reported, but never proposed for deletion. Personal data and
# credentials live here.
PROTECTED = [
    "Library/Mobile Documents",       # iCloud Drive
    "Library/Mail",
    "Library/Messages",
    "Library/Photos",
    "Pictures",
    "Documents",
    "Desktop",
    "Movies",
    "Music",
    ".ssh",
    ".gnupg",
    ".aws",
    ".config/gcloud",
    "Library/Keychains",
]

SAFETY_RANK = {SAFE: 0, REVIEW: 1, KEEP: 2}


def is_protected(rel_path):
    """True if this path is inside somewhere Reclaim must never delete from."""
    rel = rel_path.strip("/")
    for p in PROTECTED:
        if rel == p or rel.startswith(p + "/"):
            return True
    return False
