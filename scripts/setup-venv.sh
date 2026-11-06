#!/usr/bin/env bash
# Install the system packages rmdesk needs and rmdesk itself, in ~/.local/share/rmdesk/venv.
# --dev also installs the test tools (pytest, pytest-qt) for running the test suite.
set -euo pipefail

PKGS=(pyside6 python-paramiko python-keyring python-opencv python-numpy python-requests python-websocket-client)
[[ ${1:-} == --dev ]] && PKGS+=(python-pytest python-pytest-qt)
pacman -Qq "${PKGS[@]}" >/dev/null 2>&1 || sudo pacman -S --needed "${PKGS[@]}"

DATA="$HOME/.local/share/rmdesk"
VENV="$DATA/venv"
mkdir -p -m 700 "$DATA"
chmod 700 "$DATA"  # it also holds rmdesk's log and page backups
python -m venv --system-site-packages "$VENV"
cd "$(dirname "$0")/.."
PYTHONPYCACHEPREFIX="$HOME/.cache/rmdesk/pycache" "$VENV/bin/pip" install -e .
