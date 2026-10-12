#!/usr/bin/env bash
# Install Reclaim and start it at login.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || { echo "python3 not found"; exit 1; }

echo "Installing Reclaim..."
"$PY" -m pip install --user --upgrade '.[menubar]' 2>/dev/null \
  || "$PY" -m pip install --user --upgrade --break-system-packages '.[menubar]'

BIN="$("$PY" -c 'import site,os;print(os.path.join(site.USER_BASE,"bin"))')"
export PATH="$BIN:$PATH"

echo
echo "First scan (this takes a few minutes on a full disk)..."
"$PY" -m reclaim scan

echo
"$PY" -m reclaim install
echo
echo "Reclaim is running. If 'reclaim' is not on your PATH, add this to ~/.zshrc:"
echo "  export PATH=\"$BIN:\$PATH\""
