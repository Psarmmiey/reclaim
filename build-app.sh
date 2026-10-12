#!/usr/bin/env bash
# Build Reclaim.app, swap it in, restart it, and check the new build is running.
#
#   ./build-app.sh             build and install
#   ./build-app.sh --rollback  go back to the previous build
#   ./build-app.sh --dist DIR  build a zip into DIR, install nothing (CI)
#
# RECLAIM_SIGN=adhoc skips the local signing certificate (CI has none).
set -euo pipefail
cd "$(dirname "$0")"
SRC="$PWD"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || { echo "python3 not found"; exit 1; }
APPS="${RECLAIM_APP_DIR:-$HOME/Applications}"
DEST="$APPS/Reclaim.app"
PREV="$APPS/Reclaim.app.previous"      # no .app suffix: macOS ignores it
EXE="$DEST/Contents/MacOS/Reclaim"
LABEL="com.reclaim.agent"

MODE=install
DIST=""
case "${1:-}" in
  "") ;;
  --rollback) MODE=rollback ;;
  --dist) MODE=dist; DIST="${2:?usage: ./build-app.sh --dist DIR}" ;;
  *) echo "usage: ./build-app.sh [--rollback | --dist DIR]"; exit 2 ;;
esac

# Ask the running app which build it is. Prints "version build pid" or nothing.
running() {
  "$PY" - <<'PYCODE' 2>/dev/null || true
import json, os, urllib.request
cfg = json.load(open(os.path.expanduser("~/.config/reclaim/config.json")))
url = "http://%s:%s/api/status" % (cfg.get("server_host", "127.0.0.1"), cfg.get("server_port", 8765))
req = urllib.request.Request(url, headers={"X-Reclaim-Token": cfg["server_token"]})
s = json.load(urllib.request.urlopen(req, timeout=2))
print(s.get("version", "?"), s.get("build", "?"), s.get("pid", "?"))
PYCODE
}

# Wait until the app answers with the expected build (or any build if "").
wait_for() {
  local want="$1" got=""
  for _ in $(seq 1 40); do
    got="$(running)"
    if [ -n "$got" ] && { [ -z "$want" ] || [ "$(echo "$got" | cut -d' ' -f2)" = "$want" ]; }; then
      echo "  Running Reclaim $(echo "$got" | cut -d' ' -f1) (build $(echo "$got" | cut -d' ' -f2))"
      return 0
    fi
    sleep 0.5
  done
  echo "  Reclaim did not come back with build ${want:-?} (got: ${got:-no answer})."
  echo "  Check ~/.local/share/reclaim/reclaim.log, or roll back with: ./build-app.sh --rollback"
  return 1
}

start_agent() {
  "$EXE" install >/dev/null || "$EXE" install
}

if [ "$MODE" = rollback ]; then
  [ -d "$PREV" ] || { echo "No previous build to roll back to."; exit 1; }
  echo "Rolling back to the previous build..."
  mv "$DEST" "$DEST.failed"
  mv "$PREV" "$DEST"
  rm -rf "$DEST.failed"
  start_agent
  wait_for ""
  exit $?
fi

BUILD="$(date +%Y%m%d.%H%M%S)"
SOURCE="${GITHUB_SHA:+${GITHUB_SHA:0:10}}"
[ -n "$SOURCE" ] || SOURCE="$(cd "$SRC" && find reclaim packaging -type f \( -name '*.py' -o -name '*.html' \) \
          -not -path '*/__pycache__/*' -print0 | sort -z | xargs -0 shasum | shasum | cut -c1-10)"

echo "Checking build tools..."
if ! "$PY" -c "import py2app, rumps, objc, AppKit" 2>/dev/null; then
  "$PY" -m pip install --user --quiet --upgrade py2app rumps pyobjc-framework-Cocoa 2>/dev/null \
    || "$PY" -m pip install --user --quiet --upgrade --break-system-packages \
         py2app rumps pyobjc-framework-Cocoa
fi

# Build in a scratch copy so py2app never sees pyproject.toml or old builds.
STAGE="$(mktemp -d -t reclaim-build)"
trap 'rm -rf "$STAGE"' EXIT
cp -R "$SRC/reclaim" "$STAGE/reclaim"
find "$STAGE/reclaim" -name __pycache__ -type d -prune -exec rm -rf {} +
printf 'BUILD = "%s"\nSOURCE = "%s"\n' "$BUILD" "$SOURCE" > "$STAGE/reclaim/_build.py"
cp "$SRC/packaging/setup_app.py" "$SRC/packaging/reclaim_main.py" "$STAGE/"

echo "Building Reclaim.app, build $BUILD..."
( cd "$STAGE" && "$PY" setup_app.py py2app --dist-dir "$STAGE/dist" \
    > "$STAGE/build.log" 2>&1 ) || {
  echo "py2app failed. Last lines of the log:"
  tail -30 "$STAGE/build.log"
  exit 1
}

SIGN_NAME="Reclaim Local Signing"
LOGIN_KC="$HOME/Library/Keychains/login.keychain-db"

sign_hash() {
  security find-identity -p codesigning "$LOGIN_KC" 2>/dev/null \
    | awk -v n="\"$SIGN_NAME\"" 'index($0, n) { print $2; exit }' || true
}
make_identity() {
  echo "Creating a local code-signing certificate (once only)..."
  local tmp
  tmp="$(mktemp -d -t reclaim-sign)"
  cat > "$tmp/req.cnf" <<CNF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $SIGN_NAME
[ext]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
CNF
  # /usr/bin/openssl (LibreSSL) writes a .p12 that `security` can read;
  # Homebrew's OpenSSL 3 would need -legacy.
  /usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -config "$tmp/req.cnf" -keyout "$tmp/key.pem" -out "$tmp/cert.pem" >/dev/null 2>&1 &&
  /usr/bin/openssl pkcs12 -export -name "$SIGN_NAME" -inkey "$tmp/key.pem" \
    -in "$tmp/cert.pem" -out "$tmp/id.p12" -passout pass:reclaim >/dev/null 2>&1 &&
  security import "$tmp/id.p12" -k "$LOGIN_KC" -P reclaim -T /usr/bin/codesign >/dev/null &&
  echo "macOS will ask for your password to trust it for signing Reclaim." &&
  security add-trusted-cert -r trustRoot -p codeSign -k "$LOGIN_KC" "$tmp/cert.pem"
  local ok=$?
  rm -rf "$tmp"
  return $ok
}

APP_BUILT="$STAGE/dist/Reclaim.app"
HASH=""
if [ "${RECLAIM_SIGN:-}" != "adhoc" ]; then
  HASH="$(sign_hash)"
fi
if [ -z "$HASH" ] && [ "${RECLAIM_SIGN:-}" != "adhoc" ]; then
  make_identity || echo "Could not create the certificate."
  HASH="$(sign_hash)"
fi
if [ -n "$HASH" ] && codesign --force --deep --sign "$HASH" "$APP_BUILT" 2>"$STAGE/sign.log"; then
  :
else
  [ -s "$STAGE/sign.log" ] && tail -3 "$STAGE/sign.log"
  [ "${RECLAIM_SIGN:-}" = "adhoc" ] \
    || echo "Falling back to an ad-hoc signature: Accessibility will need re-enabling after each rebuild."
  codesign --force --deep --sign - "$APP_BUILT" >/dev/null 2>&1 || true
fi

if [ "$MODE" = dist ]; then
  VERSION="$(cd "$STAGE" && "$PY" -c 'import reclaim; print(reclaim.__version__)')"
  mkdir -p "$DIST"
  ZIP="$DIST/Reclaim-$VERSION.zip"
  ditto -c -k --sequesterRsrc --keepParent "$APP_BUILT" "$ZIP"
  (cd "$DIST" && shasum -a 256 "$(basename "$ZIP")" > "$(basename "$ZIP").sha256")
  echo "Built $ZIP (build $BUILD)"
  exit 0
fi

echo "Installing..."
mkdir -p "$APPS"
# The old python3-based copy, if it is still around, goes once.
if "$PY" -m pip show reclaim-disk >/dev/null 2>&1; then
  "$PY" -m reclaim uninstall >/dev/null 2>&1 || true
  "$PY" -m pip uninstall --yes --quiet reclaim-disk 2>/dev/null \
    || "$PY" -m pip uninstall --yes --quiet --break-system-packages reclaim-disk 2>/dev/null || true
fi
# Swap: the running app keeps going from its renamed bundle until restarted.
rm -rf "$PREV"
[ -d "$DEST" ] && mv "$DEST" "$PREV"
mv "$APP_BUILT" "$DEST" 2>/dev/null || cp -R "$APP_BUILT" "$DEST"

# The CLI is a thin shim over the app, so there is only ever one copy.
BIN="$("$PY" -c 'import site,os;print(os.path.join(site.USER_BASE,"bin"))')"
mkdir -p "$BIN"
printf '#!/bin/sh\nexec "%s" "$@"\n' "$EXE" > "$BIN/reclaim"
chmod +x "$BIN/reclaim"

echo "Restarting..."
# Also stop a copy opened by hand, or it keeps the lock and the old build runs on.
pkill -f "$APPS/Reclaim.app/Contents/MacOS/Reclaim" 2>/dev/null || true
sleep 0.5
start_agent
wait_for "$BUILD" || exit 1
codesign -dr - "$DEST" 2>/dev/null | sed -n 's/^designated => /  Signed as: /p'
[ -d "$PREV" ] && echo "  Previous build kept; ./build-app.sh --rollback restores it."
