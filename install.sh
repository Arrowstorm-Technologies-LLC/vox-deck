#!/bin/sh
# Install Vox Deck for the current user from this checkout (no root needed).
# Uses the system Python with PySide6 and numpy (pacman: pyside6 python-numpy).
set -eu
root="$(cd "$(dirname "$0")" && pwd)"
bin="$HOME/.local/bin"
mkdir -p "$bin" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps" \
         "$HOME/.config/systemd/user"
for pair in "vox-deck:voxdeck.cli" "vox-deck-gui:voxdeck.app"; do
  name="${pair%%:*}"; mod="${pair##*:}"
  cat > "$bin/$name" <<EOF
#!/bin/sh
PYTHONPATH="$root/src\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m $mod "\$@"
EOF
  chmod +x "$bin/$name"
done
# Plasma's session PATH does not include ~/.local/bin, so the installed entry
# gets absolute launcher paths (the repo template keeps bare names).
sed -e "s|^Exec=vox-deck|Exec=$bin/vox-deck|" "$root/desktop/vox-deck.desktop" \
  > "$HOME/.local/share/applications/vox-deck.desktop"
cp "$root/desktop/vox-deck.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/"
cp "$root/systemd/vox-deck.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload 2>/dev/null || true
echo "Installed. Start it from the app menu, or: vox-deck-gui"
echo "Autostart at login: systemctl --user enable --now vox-deck.service"
