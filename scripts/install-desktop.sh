#!/usr/bin/env bash
# Add rmdesk to your app menu: a launcher in ~/.local/bin and a desktop entry with an icon.
# Run after scripts/setup-venv.sh. Re-running is safe.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$HOME/.local/share/rmdesk/venv"
[[ -x $VENV/bin/python ]] || { echo "Run scripts/setup-venv.sh first." >&2; exit 1; }

mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" \
         "$HOME/.local/share/icons/hicolor/scalable/apps"
launcher="$HOME/.local/bin/rmdesk"
if [[ -e $launcher ]] && ! grep -q "rmdesk/venv" "$launcher"; then
  echo "$launcher exists and isn't rmdesk's launcher; leaving it alone." >&2
  echo "Move it away and run this again." >&2
  exit 1
fi
cat > "$launcher" <<LAUNCH
#!/usr/bin/env bash
export PYTHONPYCACHEPREFIX="\$HOME/.cache/rmdesk/pycache"  # keep the source folder clean
exec "$VENV/bin/python" -m rmdesk "\$@"
LAUNCH
chmod +x "$launcher"
install -m 644 "$here/data/rmdesk.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/rmdesk.svg"
cat > "$HOME/.local/share/applications/rmdesk.desktop" <<DESK
[Desktop Entry]
Name=rmdesk
Comment=reMarkable 2: files, live screen, control, and ink on notebook pages
Exec="$launcher"
Icon=rmdesk
Terminal=false
Type=Application
Categories=Utility;
Keywords=remarkable;tablet;rm2;notebook;screen;
StartupWMClass=rmdesk
DESK
command -v update-desktop-database >/dev/null && update-desktop-database "$HOME/.local/share/applications" || true
echo "Installed: run 'rmdesk' or pick rmdesk in your app menu."
