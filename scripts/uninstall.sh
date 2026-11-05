#!/usr/bin/env bash
# Remove rmdesk from this computer: the launcher, the app menu entry, the icon and the Python
# environment. Your settings, logs and page backups in ~/.local/share/rmdesk and
# ~/.config/rmdesk stay unless you pass --purge. System packages are left installed.
set -euo pipefail
purge=0
[[ ${1:-} == --purge ]] && purge=1

rm -f "$HOME/.local/share/applications/rmdesk.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/rmdesk.svg"
if [[ -f $HOME/.local/bin/rmdesk ]] && grep -q "rmdesk/venv" "$HOME/.local/bin/rmdesk"; then
  rm -f "$HOME/.local/bin/rmdesk"  # only the launcher install-desktop.sh wrote
fi
rm -rf "$HOME/.local/share/rmdesk/venv"
command -v update-desktop-database >/dev/null && update-desktop-database "$HOME/.local/share/applications" || true

if (( purge )); then
  rm -rf "$HOME/.local/share/rmdesk" "$HOME/.config/rmdesk"
  echo "rmdesk removed, with its settings, logs and backups."
  echo "A tablet password you saved is in your keyring under \"rmdesk\"; remove it there if you like."
else
  echo "rmdesk removed. Settings, logs and backups are still in ~/.local/share/rmdesk and ~/.config/rmdesk."
  echo "Run with --purge to delete those too."
fi
