#!/usr/bin/env bash
# The rmdesk bar button: focus rmdesk if it is open, start it if it is installed, otherwise
# open a terminal that offers to install it (scripts/install.sh asks before changing anything).
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$HOME/.local/share/rmdesk/venv"

if [[ -x $VENV/bin/python ]] && "$VENV/bin/python" -c "import rmdesk" 2>/dev/null; then
  # compiled Python goes to ~/.cache: written into this folder it would reload the bar widget
  run="env PYTHONPYCACHEPREFIX=$(printf '%q' "$HOME/.cache/rmdesk/pycache") $(printf '%q' "$VENV/bin/python") -m rmdesk"
  command -v uwsm-app >/dev/null 2>&1 && run="uwsm-app -- $run"
  if command -v omarchy-launch-or-focus >/dev/null 2>&1; then
    exec omarchy-launch-or-focus rmdesk "$run"  # the window's class is "rmdesk"
  fi
  eval "exec setsid -f $run"
fi

installer="$(printf '%q' "$here/scripts/install.sh")"
if command -v omarchy-launch-floating-terminal-with-presentation >/dev/null 2>&1; then
  exec omarchy-launch-floating-terminal-with-presentation "$installer"
fi
exec xdg-terminal-exec "$here/scripts/install.sh"
