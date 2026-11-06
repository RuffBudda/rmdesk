#!/usr/bin/env bash
# Install rmdesk from this folder: the Python environment (scripts/setup-venv.sh) and the app
# menu entry (scripts/install-desktop.sh). Asks before changing anything. Re-running is safe.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"

cat <<INFO
rmdesk installer

This will:
  - install the system packages rmdesk needs with pacman (asks for your password):
    pyside6, python-paramiko, python-keyring, python-opencv, python-numpy,
    python-requests, python-websocket-client
  - create a Python environment in ~/.local/share/rmdesk/venv and install rmdesk
    from $here into it. That downloads rmscene, Hershey-Fonts and (on Python
    older than 3.14) zstandard from PyPI, and rmc from GitHub (pinned to commit
    da87813a31496d156ca6ea8a27bf5128670fb45a)
  - add ~/.local/bin/rmdesk, an app menu entry and an icon

Nothing on your tablet is changed.
INFO
read -r -p "Continue? [y/N] " answer
[[ $answer == [yY]* ]] || { echo "Nothing was changed."; exit 0; }

"$here/scripts/setup-venv.sh"
"$here/scripts/install-desktop.sh"
echo
echo "Done. Open rmdesk from the bar button or your app menu."
read -r -p "Press Enter to close." _ || true
