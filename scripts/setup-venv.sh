#!/usr/bin/env bash
set -euo pipefail

PKGS=(pyside6 python-paramiko python-keyring python-opencv python-numpy python-requests python-websocket-client python-pytest python-pytest-qt)
pacman -Qq "${PKGS[@]}" >/dev/null 2>&1 || sudo pacman -S --needed "${PKGS[@]}"

VENV="$HOME/.local/share/rmdesk/venv"
python -m venv --system-site-packages "$VENV"
cd "$(dirname "$0")/.."
"$VENV/bin/pip" install -e .
