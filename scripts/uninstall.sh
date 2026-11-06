#!/usr/bin/env bash
# Remove rmdesk from this computer: the launcher, the app menu entry, the icon and the Python
# environment. Your settings, logs and page backups in ~/.local/share/rmdesk and
# ~/.config/rmdesk stay unless you pass --purge, which also offers to remove the passwords
# rmdesk saved in your keyring. System packages are left installed.
set -euo pipefail
purge=0
[[ ${1:-} == --purge ]] && purge=1

VENV="$HOME/.local/share/rmdesk/venv"
keyring_note='rmdesk saved passwords in your keyring under "rmdesk" (the tablet) and "rmdesk-stream" (screen streaming); remove them there if you like.'
if (( purge )) && [[ -x $VENV/bin/python ]]; then
  # done first: it needs rmdesk's Python environment, which is removed below
  read -r -p "Also remove the passwords rmdesk saved in your keyring? [y/N] " answer || answer=
  if [[ $answer == [yY]* ]]; then
    if "$VENV/bin/python" - <<'PY'
import keyring
from keyring.errors import PasswordDeleteError
from rmdesk import config
serial = config.load().serial
if serial:
    for service in ("rmdesk", "rmdesk-stream"):
        try:
            keyring.delete_password(service, serial)
        except PasswordDeleteError:
            pass  # nothing saved there
PY
    then keyring_note="The passwords rmdesk saved in your keyring were removed."
    fi
  fi
fi

rm -f "$HOME/.local/share/applications/rmdesk.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/rmdesk.svg"
if [[ -f $HOME/.local/bin/rmdesk ]] && grep -q "rmdesk/venv" "$HOME/.local/bin/rmdesk"; then
  rm -f "$HOME/.local/bin/rmdesk"  # only the launcher install-desktop.sh wrote
fi
rm -rf "$VENV" "$HOME/.cache/rmdesk"
command -v update-desktop-database >/dev/null && update-desktop-database "$HOME/.local/share/applications" || true

if (( purge )); then
  rm -rf "$HOME/.local/share/rmdesk" "$HOME/.config/rmdesk"
  echo "rmdesk removed, with its settings, logs and backups."
  echo "$keyring_note"
else
  echo "rmdesk removed. Settings, logs and backups are still in ~/.local/share/rmdesk and ~/.config/rmdesk."
  echo "Run with --purge to delete those too."
fi
