#!/usr/bin/env bash
# Remove Voxbox. Keeps your ~/.config/voxbox settings unless you pass --purge.
set -euo pipefail

SHARE="$HOME/.local/share/voxbox"
BIN="$HOME/.local/bin"
DESKTOP="$HOME/.local/share/applications"

command -v voxbox >/dev/null && voxbox quit >/dev/null 2>&1 || true

rm -f "$BIN/voxbox" "$BIN/voxbox-add-language"
rm -f "$DESKTOP/voxbox.desktop"
rm -rf "$SHARE"                       # app, venv, models, voices, tessdata
echo "Removed the app, venv, models and voices."

if [[ ${1:-} == --purge ]]; then
  rm -rf "$HOME/.config/voxbox"
  echo "Removed your settings too."
else
  echo "Kept ~/.config/voxbox (pass --purge to remove it)."
fi

BINDINGS="$HOME/.config/hypr/bindings.lua"
if [[ -f $BINDINGS ]] && grep -q "voxbox" "$BINDINGS"; then
  echo "Note: Voxbox keybindings are still in $BINDINGS — remove the 'Voxbox' block by hand."
fi
