#!/usr/bin/env bash
# Voxbox installer. Sets up a private Python venv (pinned, hash-verified
# dependencies), downloads the sha256-verified voice model, installs the app
# and launcher. Re-running it updates everything in place.
#
#   ./install.sh                 # English + the 7 other Kokoro languages
#   ./install.sh --languages el de ru
#   ./install.sh --all-languages # every language (~3 GB of voices)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARE="$HOME/.local/share/voxbox"
BIN="$HOME/.local/bin"
DESKTOP="$HOME/.local/share/applications"
MODEL_BASE="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }

# --- 1. system dependencies (informational; Voxbox degrades without the optional ones)
say "Checking system dependencies"
missing=()
for cmd in python3 ffmpeg; do command -v "$cmd" >/dev/null || missing+=("$cmd"); done
if ((${#missing[@]})); then
  warn "Required commands missing: ${missing[*]}"
  warn "Install them first (Arch: sudo pacman -S python ffmpeg) and re-run."
  exit 1
fi
opt_missing=()
for cmd in grim slurp tesseract wl-paste hyprpicker pdftotext; do command -v "$cmd" >/dev/null || opt_missing+=("$cmd"); done
if ((${#opt_missing[@]})); then
  warn "Optional tools missing (some features degrade): ${opt_missing[*]}"
  warn "  grim slurp hyprpicker tesseract  -> screen-region OCR"
  warn "  wl-clipboard (wl-paste)          -> read selection"
  warn "  poppler (pdftotext)              -> open PDF files"
fi

# --- 2. python venv + deps
say "Creating Python environment at $SHARE/venv"
mkdir -p "$SHARE"
# --system-site-packages so the venv can see the system PyGObject/GTK4/libadwaita,
# which are not installable from pip.
python3 -m venv --system-site-packages "$SHARE/venv"

if ! "$SHARE/venv/bin/python" - <<'PYCHECK' 2>/dev/null
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
PYCHECK
then
  warn "GTK 4 / libadwaita for Python are not available."
  warn "Install them (Arch: sudo pacman -S python-gobject gtk4 libadwaita) and re-run."
  exit 1
fi
say "Installing Python packages (pinned + hash-verified; this can take a minute)"
"$SHARE/venv/bin/pip" install --quiet --require-hashes -r "$REPO/app/requirements-pinned.txt"

# --- 3. TTS model (Kokoro), sha256-verified against app/bin/_artifacts.json
MANIFEST="$REPO/app/bin/_artifacts.json"
source "$REPO/app/bin/_fetch.sh"
say "Downloading the Kokoro voice model (~340 MB, once; sha256-verified)"
mkdir -p "$SHARE/models"
for f in kokoro-v1.0.onnx voices-v1.0.bin; do
  fetch_verified "$MODEL_BASE/$f" "$SHARE/models/$f" \
    "$(artifact_field models "$f" sha256)" "$(artifact_field models "$f" size)"
done

# --- 4. app + launcher + desktop entry
say "Installing the app"
cp "$REPO/app/voxbox.py" "$SHARE/voxbox.py"
cp "$REPO/app/bin/_langmap.py" "$SHARE/_langmap.py"
cp "$REPO/app/bin/_artifacts.json" "$SHARE/_artifacts.json"
cp "$REPO/app/bin/_fetch.sh" "$SHARE/_fetch.sh"
mkdir -p "$BIN" "$DESKTOP"

cat > "$BIN/voxbox" <<EOF
#!/usr/bin/env bash
exec "$SHARE/venv/bin/python" "$SHARE/voxbox.py" "\$@"
EOF
chmod +x "$BIN/voxbox"

install -m 0755 "$REPO/app/bin/voxbox-add-language" "$BIN/voxbox-add-language"

# launcher icon (hicolor)
for sz in 256 128 64 48; do
  ICONDIR="$HOME/.local/share/icons/hicolor/${sz}x${sz}/apps"
  mkdir -p "$ICONDIR"
  cp "$REPO/assets/icon.png" "$ICONDIR/voxbox.png"
done

cat > "$DESKTOP/voxbox.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Voxbox
Comment=Grab text off the screen and read it aloud
Exec=$BIN/voxbox
Icon=voxbox
Terminal=false
Categories=Utility;Accessibility;AudioVideo;
StartupWMClass=org.voxbox.Voxbox
Keywords=tts;speech;read;ocr;aloud;
EOF

# --- 5. extra languages
LANGS=()
case "${1:-}" in
  --languages) shift; LANGS=("$@") ;;
  --all-languages) LANGS=(--all) ;;
esac
if ((${#LANGS[@]})); then
  say "Adding extra languages"
  "$BIN/voxbox-add-language" "${LANGS[@]}" || warn "Language download failed; add them later with voxbox-add-language"
fi

echo
say "Installed. Make sure $BIN is on your PATH, then run:  voxbox"
say "Add more languages any time with:  voxbox-add-language"
