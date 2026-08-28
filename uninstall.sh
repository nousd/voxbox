#!/usr/bin/env bash
# Remove Voxbox. Keeps your ~/.config/voxbox settings unless you pass --purge.
set -euo pipefail

SHARE="$HOME/.local/share/voxbox"
BIN="$HOME/.local/bin"
DESKTOP="$HOME/.local/share/applications"

command -v voxbox >/dev/null && voxbox quit >/dev/null 2>&1 || true
pkill -f 'voxbox\.py daemon' 2>/dev/null || true

# Remove the Omarchy bar widget/plugin too, if present.
if command -v omarchy >/dev/null; then
  omarchy plugin remove io.github.nousd.voxbox --yes >/dev/null 2>&1 || true
fi

rm -f "$BIN/voxbox" "$BIN/voxbox-add-language"
rm -f "$DESKTOP/voxbox.desktop"
rm -f "$HOME"/.local/share/icons/hicolor/*/apps/voxbox.png
rm -rf "$SHARE"                       # app, venv, models, voices, tessdata
echo "Removed the app, venv, models and voices."

if [[ ${1:-} == --purge ]]; then
  rm -rf "$HOME/.config/voxbox"
  echo "Removed your settings too."
else
  echo "Kept ~/.config/voxbox (pass --purge to remove it)."
fi

