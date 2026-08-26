# Voxbox

Grab text off your screen and have it read aloud — a small, fully offline
text-to-speech reader for Linux desktops, shipped both as an
[Omarchy](https://omarchy.org) shell plugin (bar widget + native panel) and as a
standalone GTK4 app for any Wayland session.

Select a region of the screen (an image, a PDF, a video frame, unselectable UI
text), Voxbox OCRs it and reads it in a natural neural voice. Or read your
current text selection, open a document, or export the whole thing to an MP3.

![Voxbox](assets/screenshot.png)

## Why

Apps like NaturalReader and Speechify do this on Windows/macOS behind a
subscription and the cloud. Voxbox is local and free: nothing leaves your
machine, there is no account, and it themes itself to match your desktop.

## Features

- **Region → OCR → speech** — drag a box over anything on screen; it reads what's inside.
- **Read selection** — speak whatever text you've highlighted, no copy needed.
- **Open a document** — PDF, EPUB, plain text, Markdown or HTML.
- **Export to audio** — save the loaded text as MP3 / WAV / FLAC / OGG.
- **51 languages, offline** — [Kokoro](https://github.com/thewh1teagle/kokoro-onnx)
  for English + 7 major languages, [piper](https://github.com/OHF-Voice/piper1-gpl)
  for the rest.
- **Automatic language switching** — each sentence is detected and handed to a
  voice that speaks it, so mixed-language text (e.g. Greek with English terms)
  reads correctly.
- **Sentence-synced highlighting**, adjustable speed and volume, per-language
  voice memory.
- **Themed to your desktop** — reads Omarchy's colours, font and rounding, and
  re-themes live when you switch themes.

## Requirements

Required: `python3`, `python-gobject`, `gtk4`, `libadwaita`, `ffmpeg`.
Recommended (each enables a feature): `grim`, `slurp`, `hyprpicker`, `tesseract`
(screen OCR); `wl-clipboard` (read selection); `poppler` a.k.a. `pdftotext`
(open PDFs).

On Arch / Omarchy:

```bash
sudo pacman -S --needed python python-gobject gtk4 libadwaita ffmpeg \
  grim slurp hyprpicker tesseract tesseract-data-eng wl-clipboard poppler
```

## Install as an Omarchy shell plugin (recommended)

```bash
omarchy plugin add https://github.com/nousd/voxbox.git --enable
~/.config/omarchy/plugins/io.github.nousd.voxbox/install.sh   # speech engine (~600 MB, once)
```

A speaker icon appears in your bar: **click** it for the panel, **right-click**
to capture a screen region straight away, **middle-click** to play/pause.
Bind a key if you like:

```lua
o.bind("SUPER + SHIFT + R", "Voxbox", "omarchy-shell voxbox toggle")
```

The panel is a native shell panel — it follows your theme automatically. The
speech engine (Python venv + voice models) lives under `~/.local/share/voxbox`
and is shared with the standalone app below.

## Install standalone (any Wayland desktop)

```bash
git clone https://github.com/nousd/voxbox.git
cd voxbox
./install.sh                       # English + the other Kokoro languages
# ./install.sh --languages el de ru   # add specific languages
# ./install.sh --all-languages         # every language (~3 GB of voices)
```

This also installs a GTK4 app (`voxbox`) with the same features plus a file
opener and an export dialog; on Hyprland the installer offers keybindings.

Add languages any time:

```bash
voxbox-add-language            # list installable languages
voxbox-add-language el de      # add Greek and German
```

`voxbox-add-language` also fetches the matching OCR data (tessdata_fast, no
root needed), so captured Greek/German/etc. text is recognised correctly.

## Usage

Run `voxbox`, or use the keybindings (added by the installer):

| Key | Action |
| --- | --- |
| `Super`+`Shift`+`R` | Open / focus the panel |
| `Super`+`Alt`+`T` | Read the highlighted text |
| `Super`+`Alt`+`V` | Drag a box → OCR → read |
| `Super`+`Alt`+`P` | Play / pause |

Inside the panel: `Space` play/pause, `←`/`→` skip a sentence, `Ctrl`+`R`
region, `Ctrl`+`V` selection, `Ctrl`+`O` open a file, `Ctrl`+`E` export,
`Esc` hide. The text area is editable — fix an OCR slip and it re-reads. Pick a
voice per language from the dropdown; the ▶ next to it previews the voice.

## Configuration

Settings live in `~/.config/voxbox/config.json` (voice, per-language voices,
speed, volume, OCR languages). Drop custom CSS in `~/.config/voxbox/style.css`
to tweak the look; it loads on top of the generated theme. Run `voxbox theme`
to print the stylesheet Voxbox generates from your current Omarchy theme.

Environment overrides: `VOXBOX_CONFIG_DIR`, `VOXBOX_HOME`, `VOXBOX_APP_ID`.

## Uninstall

```bash
./uninstall.sh            # keeps your settings
./uninstall.sh --purge    # removes settings too
```

## How it works

- **OCR**: `grim` captures the region (frozen with `hyprpicker`), `tesseract`
  reads it. The language pack is chosen from tesseract's own script detection
  combined with the languages you use.
- **TTS**: Kokoro (ONNX) and piper run on the CPU, a sentence ahead of playback,
  so speech starts almost immediately. Audio is streamed through PipeWire.
- **Language detection**: the alphabet narrows the candidates, then
  [lingua](https://github.com/pemistahl/lingua-py) decides between languages
  that share a script. Short fragments inherit the surrounding language.

## Credits

Built on [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx),
[piper](https://github.com/OHF-Voice/piper1-gpl),
[lingua](https://github.com/pemistahl/lingua-py),
[tesseract](https://github.com/tesseract-ocr/tesseract) and GTK4/libadwaita.
Voice models carry their own licenses (Kokoro: Apache-2.0; piper voices: MIT/CC).

## License

MIT — see [LICENSE](LICENSE).
