#!/bin/sh
# Copy the Vox Deck plugin into OpenDeck's user plugin directory.
# Restart OpenDeck afterwards (or use its plugin reload) so it picks the plugin up.
set -eu
src="$(cd "$(dirname "$0")" && pwd)/xyz.arrowstorm.voxdeck.sdPlugin"
dest="${XDG_CONFIG_HOME:-$HOME/.config}/opendeck/plugins"
mkdir -p "$dest"
rm -rf "$dest/xyz.arrowstorm.voxdeck.sdPlugin"
cp -r "$src" "$dest/"
chmod +x "$dest/xyz.arrowstorm.voxdeck.sdPlugin/plugin.py"
echo "Installed to $dest/xyz.arrowstorm.voxdeck.sdPlugin"
