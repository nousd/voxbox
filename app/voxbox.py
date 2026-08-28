#!/usr/bin/env python3
"""voxbox - pop up, grab text off the screen, read it aloud.

Entry points (see ~/.local/bin/voxbox):
    voxbox              show the panel
    voxbox region       drag a box, OCR it, read it
    voxbox selection    read the highlighted text
    voxbox read         read stdin
    voxbox stop         stop playback
    voxbox toggle       play/pause
    voxbox quit         close the running instance
    voxbox theme        print the CSS generated from the current Omarchy theme

The panel is themed from Omarchy's colors.toml and re-themes itself live when
the theme or font changes. Set VOXBOX_APP_ID to run a second, separate instance.
"""

import json
import os
import errno
import re
import select
import stat as stat_module
import subprocess
import sys
import threading
import time
from pathlib import Path
from shutil import which

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402

APP_ID = os.environ.get("VOXBOX_APP_ID", "org.voxbox.Voxbox")
CODE_PATH = Path(__file__).resolve()
CODE_MTIME = CODE_PATH.stat().st_mtime  # what this process was started from
HOME = Path.home()
SHARE = HOME / ".local/share/voxbox"
MODELS = SHARE / "models"
VOICES_DIR = SHARE / "voices"   # piper .onnx voices
TESSDATA_DIR = SHARE / "tessdata" # tesseract language packs (tessdata_fast), user-installed
# VOXBOX_CONFIG_DIR isolates a test instance's settings from the real ones.
CONFIG_DIR = Path(os.environ.get("VOXBOX_CONFIG_DIR") or (HOME / ".config/voxbox"))
CONFIG_PATH = CONFIG_DIR / "config.json"
USER_CSS_PATH = CONFIG_DIR / "style.css"
# VOXBOX_OMARCHY_STATE lets a test instance point at a scratch theme directory.
OMARCHY_STATE = Path(os.environ.get("VOXBOX_OMARCHY_STATE") or (HOME / ".local/state/omarchy/current"))
OMARCHY_THEME_NAME = OMARCHY_STATE / "theme.name"
OMARCHY_COLORS = OMARCHY_STATE / "theme/colors.toml"
OMARCHY_SHELL_TOML = OMARCHY_STATE / "theme/shell.toml"
FONTCONFIG = HOME / ".config/fontconfig/fonts.conf"
SR = 24000  # every engine is resampled to this

# Resource ceilings. Capture, OCR, clipboard, documents and daemon events are
# hard-bounded so oversized input degrades to a small error event instead of
# exhausting Voxbox or the shell that parses its output.
MAX_IMAGE_BYTES = 48 * 1024 * 1024      # region screenshot (PNG from grim)
MAX_OCR_BYTES = 4 * 1024 * 1024         # tesseract text output
MAX_TEXT_CHARS = 100_000                # loaded text (~2 hours of speech)
MAX_FILE_BYTES = 50 * 1024 * 1024       # source document size
MAX_SENTENCES = 1_500
MAX_SENTENCE_CHARS = 400                # longer chunks are hard-split
MAX_EVENT_BYTES = 1024 * 1024           # any single daemon JSON line
MAX_EPUB_MEMBERS = 1000                 # archive entry-count ceiling


class InputTooLarge(ValueError):
    """An input exceeded its ceiling; the operation was abandoned."""


def run_capped(cmd, cap, input_bytes=None, timeout=120):
    """Run a helper reading at most `cap` bytes of its stdout.

    The producer-side ceiling: overflow (or a hung helper) kills the process
    and raises InputTooLarge, so oversized data never accumulates.
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    if input_bytes is not None:
        def _feed():
            try:
                proc.stdin.write(input_bytes)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        threading.Thread(target=_feed, daemon=True).start()
    out = bytearray()
    deadline = time.time() + timeout
    fd = proc.stdout
    try:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise InputTooLarge(f"{cmd[0]} timed out")
            ready, _, _ = select.select([fd], [], [], min(remaining, 1.0))
            if not ready:
                continue
            chunk = fd.read1(65536)
            if not chunk:
                break
            out += chunk
            if len(out) > cap:
                raise InputTooLarge(f"{cmd[0]} output exceeded {cap // (1024*1024)} MB limit")
    except InputTooLarge:
        proc.kill()
        proc.wait()
        raise
    proc.wait(timeout=10)
    return bytes(out)
PREVIEW_LINES = {
    "en": "This is how I sound when I read your screen.",
    "el": "Έτσι ακούγομαι όταν διαβάζω την οθόνη σου.",
    "de": "So klinge ich, wenn ich deinen Bildschirm vorlese.",
    "fr": "Voici ma voix quand je lis votre écran.",
    "es": "Así sueno cuando leo tu pantalla.",
    "it": "Ecco come suono quando leggo il tuo schermo.",
    "pt": "É assim que eu soo quando leio a sua tela.",
    "nl": "Zo klink ik als ik je scherm voorlees.",
    "sv": "Så här låter jag när jag läser din skärm.",
    "da": "Sådan lyder jeg, når jeg læser din skærm.",
    "no": "Slik høres jeg ut når jeg leser skjermen din.",
    "fi": "Tältä kuulostan, kun luen näyttöäsi.",
    "pl": "Tak brzmię, kiedy czytam twój ekran.",
    "cs": "Takhle zním, když čtu tvou obrazovku.",
    "sk": "Takto zniem, keď čítam tvoju obrazovku.",
    "hu": "Így hangzom, amikor felolvasom a képernyődet.",
    "ro": "Așa sun când îți citesc ecranul.",
    "tr": "Ekranını okurken böyle duyuluyorum.",
    "ru": "Вот как я звучу, когда читаю твой экран.",
    "uk": "Ось як я звучу, коли читаю твій екран.",
    "bg": "Така звуча, когато чета екрана ти.",
    "sr": "Овако звучим када читам твој екран.",
    "ar": "هكذا أبدو عندما أقرأ شاشتك.",
    "fa": "این صدای من است وقتی صفحه شما را می‌خوانم.",
    "he": "כך אני נשמע כשאני קורא את המסך שלך.",
    "hi": "जब मैं आपकी स्क्रीन पढ़ता हूँ तो मैं ऐसा सुनाई देता हूँ।",
    "ja": "あなたの画面を読むとき、私はこんな声です。",
    "zh": "这就是我朗读你屏幕时的声音。",
    "ko": "이것이 제가 화면을 읽을 때의 목소리입니다.",
    "vi": "Đây là giọng của tôi khi đọc màn hình của bạn.",
    "id": "Seperti inilah suara saya saat membaca layar Anda.",
    "ca": "Així sono quan llegeixo la teva pantalla.",
    "eu": "Honela entzuten naiz zure pantaila irakurtzen dudanean.",
    "cy": "Dyma sut rydw i'n swnio wrth ddarllen eich sgrin.",
    "is": "Svona hljóma ég þegar ég les skjáinn þinn.",
    "et": "Nii ma kõlan, kui loen sinu ekraani.",
    "lv": "Tā es skanu, kad lasu tavu ekrānu.",
    "sl": "Tako zvenim, ko berem tvoj zaslon.",
    "sq": "Kështu tingëlloj kur lexoj ekranin tënd.",
    "sw": "Hivi ndivyo ninavyosikika ninaposoma skrini yako.",
    "kk": "Экраныңды оқығанда мен осылай естілемін.",
    "hy": "Ահա թե ինչպես եմ հնչում, երբ կարդում եմ քո էկրանը։",
    "ka": "ასე ვჟღერ, როცა შენს ეკრანს ვკითხულობ.",
    "ur": "جب میں آپ کی اسکرین پڑھتا ہوں تو میں ایسا سنائی دیتا ہوں۔",
    "bn": "আমি যখন আপনার স্ক্রিন পড়ি তখন আমার কণ্ঠ এমন শোনায়।",
    "te": "నేను మీ స్క్రీన్ చదివేటప్పుడు నా గొంతు ఇలా వినిపిస్తుంది.",
    "ml": "ഞാൻ നിങ്ങളുടെ സ്ക്രീൻ വായിക്കുമ്പോൾ എന്റെ ശബ്ദം ഇങ്ങനെയാണ്.",
    "mr": "मी तुमची स्क्रीन वाचतो तेव्हा माझा आवाज असा येतो.",
    "ne": "म तपाईंको स्क्रिन पढ्दा यस्तो सुनिन्छु।",
    "lb": "Sou kléngen ech, wann ech däin Écran virliesen.",
    "ku": "Bi vî rengî deng dikim dema ekrana te dixwînim.",
}

# voices_by_lang: the voice to use for each language when auto-detect switches
# away from the selected voice; filled in as the user picks voices.
DEFAULTS = {
    "voice": "af_heart", "voices_by_lang": {},
    "speed": 1.0, "volume": 0.85, "ocr_langs": "eng",
}


# --------------------------------------------------------------------------- config


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except Exception:
        pass
    # Older configs kept one Greek and one Latin voice; fold them in.
    by_lang = cfg.setdefault("voices_by_lang", {})
    for old_key, lang in (("voice_el", "el"), ("voice_latin", "en")):
        if old_key in cfg:
            by_lang.setdefault(lang, cfg.pop(old_key))
    return cfg


def save_config(cfg):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


# --------------------------------------------------------------------------- theme


def _hex_to_rgb(value):
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a, b, amount):
    """Same blend omarchy-theme-set-templates uses for `{{ mix a b N% }}`."""
    ra, ga, ba = _hex_to_rgb(a)
    rb, gb, bb = _hex_to_rgb(b)
    r = round(ra * (1 - amount) + rb * amount)
    g = round(ga * (1 - amount) + gb * amount)
    b_ = round(ba * (1 - amount) + bb * amount)
    return f"#{r:02x}{g:02x}{b_:02x}"


def _rgba(value, alpha):
    r, g, b = _hex_to_rgb(value)
    return f"rgba({r}, {g}, {b}, {max(0.0, min(1.0, alpha)):.3f})"


def _darker(value, factor):
    """Qt.darker(): divide HSV value by `factor` - the shell's dim-text recipe."""
    r, g, b = _hex_to_rgb(value)
    return "#%02x%02x%02x" % tuple(int(round(c / factor)) for c in (r, g, b))


def _is_hex(value):
    return bool(re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", (value or "").strip()))


class OmarchyTheme:
    """Palette, shell tokens and font from the current Omarchy theme, as GTK CSS.

    Colors come from `omarchy-theme-color --all` (the resolver every Omarchy
    template goes through), then colors.toml directly. Sizes, state alphas,
    border widths and surface colors come from the theme's generated
    shell.toml - the same file the Omarchy shell reads for its panels - so
    the panel follows a theme's overrides exactly. Without Omarchy the stock
    libadwaita look stays.
    """

    FONT_SCALE = {
        "caption": 0.833, "body-small": 0.917, "body": 1.0, "subtitle": 1.083,
        "title": 1.167, "heading": 1.333, "display": 2.0, "display-large": 2.333,
        "icon-large": 1.5,
    }
    SPACING = {
        "xxs": 2, "xs": 3, "sm": 4, "md": 6, "lg": 8, "xl": 10, "xxl": 12, "xxxl": 14, "huge": 18,
        "control-gap": 8, "control-padding-x": 10, "control-padding-y": 6, "input-padding-y": 7,
        "control-height": 28, "popup-row-height": 28, "row-gap": 8, "row-padding-x": 12,
        "label-gap": 4, "panel-gap": 14, "panel-padding": 18, "popup-padding": 14,
    }

    def __init__(self):
        self.palette = {}
        self.shell = {}
        self.tokens = {}
        self.font = None
        self.rounding = 0
        self.provider = None
        self._listeners = []
        self._monitors = []
        self._debounce = None

    # -- discovery ----------------------------------------------------------

    def refresh(self):
        self.palette = self._read_palette()
        if self.palette:
            try:
                self.palette["name"] = OMARCHY_THEME_NAME.read_text().strip()
            except Exception:
                self.palette["name"] = ""
        self.shell = self._read_shell_toml()
        self.font = self._read_font()
        self.rounding = self._read_rounding()
        self.tokens = self._build_tokens() if self.palette else {}
        return bool(self.palette)

    @staticmethod
    def _read_palette():
        pal = {}
        if which("omarchy-theme-color") and OMARCHY_COLORS.exists():
            try:
                out = subprocess.run(
                    ["omarchy-theme-color", "--file", str(OMARCHY_COLORS), "--all"],
                    capture_output=True, text=True, timeout=5,
                ).stdout
                for line in out.splitlines():
                    if "\t" in line:
                        key, value = line.split("\t", 1)
                        pal[key.strip()] = value.strip()
            except Exception:
                pal = {}
        if not pal.get("background") and OMARCHY_COLORS.exists():
            try:
                import tomllib

                raw = tomllib.loads(OMARCHY_COLORS.read_text())
                pal = {k: str(v) for k, v in raw.items()}
            except Exception:
                pal = {}
        if not _is_hex(pal.get("background")) or not _is_hex(pal.get("foreground")):
            return {}

        bg, fg = pal["background"][:7], pal["foreground"][:7]
        pal["background"], pal["foreground"] = bg, fg
        # Fill anything a sparse theme leaves out with the same derivations
        # Omarchy applies, so old themes still render sensibly.
        pal.setdefault("accent", pal.get("blue", fg))
        pal.setdefault("selection", _mix(bg, fg, 0.25))
        pal.setdefault("selection_foreground", pal.get("bright_foreground", fg))
        pal.setdefault("muted", _mix(bg, fg, 0.35))
        pal.setdefault("red", "#e06c75")
        pal.setdefault("green", "#98c379")
        pal.setdefault("yellow", "#e5c07b")
        if pal.get("mode") not in ("dark", "light"):
            pal["mode"] = pal.get("theme_type") if pal.get("theme_type") in ("dark", "light") else None
        if pal["mode"] is None:
            r, g, b = _hex_to_rgb(bg)
            pal["mode"] = "light" if (0.299 * r + 0.587 * g + 0.114 * b) > 140 else "dark"
        return pal

    @staticmethod
    def _read_shell_toml():
        try:
            import tomllib

            return tomllib.loads(OMARCHY_SHELL_TOML.read_text())
        except Exception:
            return {}

    @staticmethod
    def _read_font():
        # fontconfig is Omarchy's source of truth for the UI font (`omarchy font set`).
        try:
            out = subprocess.run(
                ["fc-match", "monospace", "-f", "%{family}"], capture_output=True, text=True, timeout=3
            ).stdout
            family = out.split(",")[0].strip()
            return family or None
        except Exception:
            return None

    @staticmethod
    def _read_rounding():
        try:
            out = subprocess.run(
                ["hyprctl", "-j", "getoption", "decoration:rounding"],
                capture_output=True, text=True, timeout=3,
            ).stdout
            return max(0, int(json.loads(out).get("int", 0)))
        except Exception:
            return 0

    # -- shell.toml resolution ----------------------------------------------

    def _shell_get(self, section, key, default=None):
        value = self.shell.get(section, {}).get(key)
        return default if value is None or value == "" else value

    def _shell_num(self, section, key, default):
        try:
            return float(self._shell_get(section, key, default))
        except (TypeError, ValueError):
            return default

    def _shell_color(self, value, fallback, depth=0):
        """Resolve a shell.toml colour: hex, palette role, `section.key`
        reference or a Hyprland-style gradient (first stop wins, like the
        shell's flatColor)."""
        if depth > 6 or value is None:
            return fallback
        parts = [t for t in str(value).split() if not re.fullmatch(r"-?\d+(\.\d+)?deg", t)]
        token = (parts[0] if parts else str(value)).strip()
        low = token.lower()
        if _is_hex(token):
            return token[:7]
        m = re.fullmatch(r"rgba?\(([0-9a-fA-F]{6})[0-9a-fA-F]{0,2}\)", token)
        if m:
            return "#" + m.group(1)
        if "." in low:
            section, key = low.split(".", 1)
            ref = self._shell_get(section, key)
            return self._shell_color(ref, fallback, depth + 1) if ref else fallback
        p = self.palette
        roles = {
            "foreground": p["foreground"], "text": p["foreground"], "accent": p["accent"],
            "background": p["background"], "muted": p["muted"], "urgent": p["red"],
        }
        return roles.get(low, fallback)

    def _build_tokens(self):
        p = self.palette
        bg, fg, accent = p["background"], p["foreground"], p["accent"]
        g, n, c = self._shell_get, self._shell_num, self._shell_color

        base = max(1.0, n("font", "base-size", 12))
        font_scale = base / 12.0
        fonts = {}
        for key, mult in self.FONT_SCALE.items():
            fonts[key] = int(round(n("font", key, round(base * mult))))
        fonts["icon"] = int(round(n("font", "icon", fonts["title"])))
        fonts["icon-small"] = int(round(n("font", "icon-small", fonts["body-small"])))

        space_scale = n("spacing", "scale", 1.0) * (font_scale if g("spacing", "scale-with-font", True) else 1.0)
        spacing = {}
        for key, default in self.SPACING.items():
            spacing[key] = int(round(n("spacing", key, default * space_scale)))

        def state(name, fill_default, border_alpha_default, border_width_default):
            color = c(g("controls", f"{name}-color", "foreground"), fg)
            return {
                "fill": _rgba(color, n("controls", f"{name}-fill-alpha", fill_default)),
                "border": _rgba(
                    c(g("controls", f"{name}-border", "foreground"), fg),
                    n("controls", f"{name}-border-alpha", border_alpha_default),
                ),
                "border_width": int(round(n("controls", f"{name}-border-width", border_width_default))),
                "color": color,
            }

        return {
            "bg": bg, "fg": fg, "accent": accent,
            "dim": _darker(fg, 1.4), "placeholder": _darker(fg, 1.6), "disabled": _darker(fg, 2.0),
            "radius": self.rounding,
            "font": fonts, "space": spacing,
            "normal": state("normal", 0.04, 0.4, 1),
            "hover": state("hover-cursor", 0.08, 0.25, 1),
            "focus": state("focus", 0.08, 0.25, 1),
            "selected": state("selected", 0.18, 1.0, 0),
            "pressed_fill": _rgba(c(g("controls", "pressed-color", "foreground"), fg), n("controls", "pressed-fill-alpha", 0.22)),
            "selection_fill": _rgba(c(g("controls", "selection-color", "foreground"), fg), n("controls", "selection-fill-alpha", 0.35)),
            "separator": _rgba(fg, 0.12),
            "popup_bg": _rgba(c(g("popups", "background", "background"), bg), n("popups", "background-alpha", 1.0)),
            "popup_text": c(g("popups", "text", "foreground"), fg),
            "popup_border": _rgba(c(g("popups", "border", "accent"), accent), n("popups", "border-alpha", 1.0)),
            "tooltip_bg": _rgba(c(g("tooltip", "background", "background"), bg), n("tooltip", "background-alpha", 1.0)),
            "tooltip_text": c(g("tooltip", "text", "foreground"), fg),
            "tooltip_border": _rgba(c(g("tooltip", "border", "foreground"), fg), n("tooltip", "border-alpha", 1.0)),
            "menu_selected_bg": _rgba(c(g("menu", "selected-background", "foreground"), fg), n("menu", "selected-background-alpha", 0.08)),
            "menu_selected_text": c(g("menu", "selected-text", "accent"), accent),
            # The lock screen's text-selection tint doubles as the "now reading" band.
            "reading_bg": _mix(bg, c(g("lock", "selection", "accent"), accent), n("lock", "selection-alpha", 0.45)),
            "mode": p["mode"],
        }

    # -- css ----------------------------------------------------------------

    def css(self):
        t = self.tokens
        if not t:
            return ""
        f, sp = t["font"], t["space"]
        bg, fg, accent = t["bg"], t["fg"], t["accent"]
        r = t["radius"]
        nb, hb, fb, sel = t["normal"], t["hover"], t["focus"], t["selected"]
        font = f'font-family: "{self.font}";' if self.font else ""
        control_h = sp["control-height"]
        knob = max(14, round(control_h * 0.38))
        track = max(4, round(control_h * 0.11))
        action = max(round(22 * (sp["control-height"] / 28)), f["icon"] + sp["sm"] * 2)

        named = {
            "window_bg_color": bg, "window_fg_color": fg, "view_bg_color": bg, "view_fg_color": fg,
            "headerbar_bg_color": bg, "headerbar_fg_color": fg, "headerbar_backdrop_color": bg,
            "card_bg_color": bg, "card_fg_color": fg, "dialog_bg_color": bg, "dialog_fg_color": fg,
            "popover_bg_color": bg, "popover_fg_color": fg, "sidebar_bg_color": bg, "sidebar_fg_color": fg,
            "accent_bg_color": accent, "accent_fg_color": bg, "accent_color": accent,
            "destructive_bg_color": self.palette["red"], "destructive_color": self.palette["red"],
            "error_bg_color": self.palette["red"], "error_color": self.palette["red"],
            "warning_bg_color": self.palette["yellow"], "warning_color": self.palette["yellow"],
            "success_bg_color": self.palette["green"], "success_color": self.palette["green"],
            "shade_color": t["separator"], "scrollbar_outline_color": "transparent",
        }
        defines = "\n".join(f"@define-color {k} {v};" for k, v in named.items())
        variables = "\n".join(f"  --{k.replace('_', '-')}: {v};" for k, v in named.items())

        return f"""
/* generated by voxbox from the Omarchy theme "{self.palette.get('name', '')}" (shell.toml tokens) */
{defines}
:root {{
{variables}
  --window-radius: {r}px;
  --popover-radius: {r}px;
}}

window, popover, tooltip {{ {font} font-size: {f['body']}px; }}
window {{ background: {bg}; color: {fg}; }}
window.csd, .csd, decoration {{ border-radius: {r}px; box-shadow: none; outline: none; margin: 0; }}
label {{ color: {fg}; }}
label:disabled, button:disabled label {{ color: {t['disabled']}; }}
*:focus-visible {{ outline: {fb['border_width']}px solid {fb['border']}; outline-offset: -{fb['border_width']}px; }}

/* panel scaffolding, mirroring the shell's Panel/PanelHero/PanelSectionHeader */
.panel {{ padding: {sp['panel-padding']}px; }}
.hero-icon {{ font-size: {f['display']}px; color: {fg}; }}
.hero-title {{ font-size: {f['title']}px; font-weight: bold; color: {fg}; }}
.hero-meta, .section-header, .section-value {{
  font-size: {f['caption']}px; font-weight: bold; color: {t['dim']}; letter-spacing: 0.08em;
}}
.section-header {{ padding-top: {max(1, round(f['caption'] * 0.15))}px; }}
.separator {{ background: {t['separator']}; min-height: 1px; }}
.caption {{ font-size: {f['caption']}px; color: {t['dim']}; }}
.body-small {{ font-size: {f['body-small']}px; }}

/* controls: Ui/Button.qml and PanelActionButton.qml */
button {{
  background: transparent; color: {fg};
  border: {nb['border_width']}px solid transparent; border-radius: {r}px;
  box-shadow: none; text-shadow: none; -gtk-icon-shadow: none;
  min-height: {control_h - 2 * nb['border_width']}px; min-width: 0;
  padding: 0 {sp['control-padding-x']}px;
  font-size: {f['body']}px;
  transition: background-color 120ms, border-color 120ms;
}}
button.bordered {{ background: {nb['fill']}; border-color: {nb['border']}; }}
button:hover {{ background: {hb['fill']}; border-color: {hb['border']}; }}
button:active {{ background: {t['pressed_fill']}; }}
button.selected, button:checked {{ background: {sel['fill']}; border-color: {sel['border'] if sel['border_width'] else 'transparent'}; }}
button.selected label, button:checked label {{ color: {sel['color']}; }}
button:disabled {{ background: transparent; border-color: {_rgba(fg, 0.15)}; }}
button label {{ color: {fg}; }}
button .icon {{ font-size: {f['icon']}px; }}
button.action {{ min-width: {action - 2 * nb['border_width']}px; min-height: {action - 2 * nb['border_width']}px; padding: 0; }}
button.action .icon {{ font-size: {f['icon']}px; }}
button.transport {{ min-width: {control_h + sp['md'] - 2 * nb['border_width']}px; padding: 0; }}
button.chip {{ min-height: {f['caption'] + 2 * sp['sm']}px; padding: 0 {sp['lg']}px; }}
button.chip label {{ font-size: {f['caption']}px; font-weight: bold; letter-spacing: 0.08em; }}
button.chip .icon {{ font-size: {f['caption'] + 2}px; }}
button.transport .icon {{ font-size: {f['icon-large']}px; }}

/* the reading pane: Ui/TextField.qml */
.reader {{
  background: {nb['fill']}; border: {nb['border_width']}px solid {nb['border']}; border-radius: {r}px;
  box-shadow: none;
}}
.reader:hover {{ border-color: {hb['border']}; }}
.reader:focus-within {{ background: {fb['fill']}; border-color: {fb['border']}; }}
textview, textview > text {{ background: transparent; color: {fg}; caret-color: {fg}; font-size: {f['subtitle']}px; }}
textview > text > selection, textview > text selection {{ background: {t['selection_fill']}; color: {fg}; }}

/* sliders: Ui/PanelSlider.qml - foreground fill, foreground knob rimmed in background */
scale {{ padding: {sp['sm']}px 0; }}
scale trough {{ background: {sel['fill']}; border: none; box-shadow: none; min-height: {track}px; border-radius: {track}px; }}
scale highlight {{ background: {fg}; border: none; border-radius: {track}px; min-height: {track}px; }}
scale slider {{
  background: {fg}; border: 2px solid {bg}; border-radius: 50%; box-shadow: none;
  min-width: {knob}px; min-height: {knob}px; margin: -{max(0, (knob - track) // 2)}px 0;
}}
scale slider:hover {{ background: {fg}; }}
scale marks indicator {{ background: {bg}; min-height: {track + 4}px; min-width: 2px; }}

/* dropdown: Ui/Dropdown.qml trigger + popup list in the popups surface */
dropdown > button {{ background: {nb['fill']}; border-color: {nb['border']}; padding-right: {sp['md']}px; }}
dropdown > button:hover {{ background: {hb['fill']}; border-color: {hb['border']}; }}
dropdown > button:focus-visible {{ background: {fb['fill']}; border-color: {fb['border']}; outline: none; }}
dropdown > button > box {{ padding: 0; }}
dropdown > button label {{ color: {fg}; }}
dropdown > button arrow {{ color: {t['dim']}; -gtk-icon-size: {f['body']}px; }}
popover {{ background: transparent; }}
popover > contents {{
  background: {t['popup_bg']}; color: {t['popup_text']};
  border: {max(1, nb['border_width'])}px solid {t['popup_border']}; border-radius: {r}px;
  box-shadow: none; padding: {sp['xs']}px 0;
}}
popover > arrow {{ background: {t['popup_bg']}; border: 1px solid {t['popup_border']}; }}
popover.menu listview, popover listview {{ background: transparent; }}
popover listview > row {{
  color: {t['popup_text']}; border-radius: {r}px; min-height: {sp['popup-row-height']}px;
  padding: 0 {sp['row-padding-x']}px; margin: 0 {sp['xs']}px;
}}
popover listview > row:hover {{ background: {hb['fill']}; }}
popover listview > row:selected {{ background: {t['menu_selected_bg']}; color: {t['menu_selected_text']}; }}
popover listview > row:selected label, popover listview > row:selected image {{ color: {t['menu_selected_text']}; }}
popover listview > row image.checkmark {{ color: {t['menu_selected_text']}; -gtk-icon-size: {f['body']}px; }}

tooltip, tooltip.background {{
  background: {t['tooltip_bg']}; color: {t['tooltip_text']};
  border: {max(1, nb['border_width'])}px solid {t['tooltip_border']}; border-radius: 0;
  box-shadow: none; padding: {sp['control-padding-y']}px {sp['control-padding-x']}px; font-size: {f['body-small']}px;
}}
popover entry, popover searchbar entry, popover .dropdown-searchbar entry {{
  background: {nb['fill']}; color: {fg}; border: {nb['border_width']}px solid {nb['border']}; border-radius: {r}px;
  min-height: {control_h - 2 * nb['border_width']}px; padding: 0 {sp['control-padding-x']}px; box-shadow: none; caret-color: {fg};
}}
popover entry:focus-within {{ border-color: {fb['border']}; background: {fb['fill']}; }}
popover entry image {{ color: {t['dim']}; }}
popover .dropdown-searchbar {{ padding: {sp['xs']}px {sp['xs']}px {sp['sm']}px; }}
scrollbar {{ background: transparent; }}
scrollbar slider {{ background: {_rgba(fg, 0.3)}; border-radius: {r}px; min-width: 6px; min-height: 6px; }}
scrollbar slider:hover {{ background: {_rgba(fg, 0.5)}; }}
"""

    # -- apply --------------------------------------------------------------

    def apply(self):
        """Load the palette and push it onto the default display. Safe to call repeatedly."""
        if not self.refresh():
            return False
        display = Gdk.Display.get_default()
        if display is None:
            return False
        if self.provider is None:
            self.provider = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_display(
                display, self.provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
            )
        css = self.css()
        try:
            css += "\n" + USER_CSS_PATH.read_text()  # personal tweaks win
        except Exception:
            pass
        self.provider.load_from_string(css)

        manager = Adw.StyleManager.get_default()
        manager.set_color_scheme(
            Adw.ColorScheme.FORCE_LIGHT if self.palette["mode"] == "light" else Adw.ColorScheme.FORCE_DARK
        )
        for cb in list(self._listeners):
            cb(self)
        return True

    def subscribe(self, callback):
        self._listeners.append(callback)

    def watch(self):
        """Re-theme live when `omarchy theme set` or `omarchy font set` runs."""
        for path in (OMARCHY_THEME_NAME, FONTCONFIG):
            try:
                mon = Gio.File.new_for_path(str(path)).monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, None)
                mon.connect("changed", self._on_file_changed)
                self._monitors.append(mon)  # keep a reference or GC silences it
            except Exception:
                pass

    def _on_file_changed(self, *_args):
        if self._debounce:
            GLib.source_remove(self._debounce)
        # theme.name is written before the app-retint hooks run; a short wait
        # lets fontconfig settle too when both change together.
        self._debounce = GLib.timeout_add(350, self._reapply)

    def _reapply(self):
        self._debounce = None
        self.apply()
        return False


THEME = OmarchyTheme()


# --------------------------------------------------------------------------- voices

# Kokoro voices, best-sounding first within each accent. The model card grades
# them; the ones marked ★ are the A/B tier and are what you want by default.
# (id, label, ISO 639-1 language, kokoro's own language code)
KOKORO_VOICES = [
    ("af_heart",   "Heart · English (US) ♀ ★", "en", "en-us"),
    ("af_bella",   "Bella · English (US) ♀ ★", "en", "en-us"),
    ("bf_emma",    "Emma · English (UK) ♀ ★", "en", "en-gb"),
    ("af_nicole",  "Nicole · English (US) ♀ ★ (soft)", "en", "en-us"),
    ("am_michael", "Michael · English (US) ♂", "en", "en-us"),
    ("am_fenrir",  "Fenrir · English (US) ♂", "en", "en-us"),
    ("am_puck",    "Puck · English (US) ♂", "en", "en-us"),
    ("af_aoede",   "Aoede · English (US) ♀", "en", "en-us"),
    ("af_kore",    "Kore · English (US) ♀", "en", "en-us"),
    ("af_sarah",   "Sarah · English (US) ♀", "en", "en-us"),
    ("af_nova",    "Nova · English (US) ♀", "en", "en-us"),
    ("af_alloy",   "Alloy · English (US) ♀", "en", "en-us"),
    ("af_jessica", "Jessica · English (US) ♀", "en", "en-us"),
    ("af_river",   "River · English (US) ♀", "en", "en-us"),
    ("af_sky",     "Sky · English (US) ♀", "en", "en-us"),
    ("am_echo",    "Echo · English (US) ♂", "en", "en-us"),
    ("am_eric",    "Eric · English (US) ♂", "en", "en-us"),
    ("am_liam",    "Liam · English (US) ♂", "en", "en-us"),
    ("am_onyx",    "Onyx · English (US) ♂", "en", "en-us"),
    ("am_adam",    "Adam · English (US) ♂", "en", "en-us"),
    ("am_santa",   "Santa · English (US) ♂", "en", "en-us"),
    ("bf_isabella", "Isabella · English (UK) ♀", "en", "en-gb"),
    ("bf_alice",   "Alice · English (UK) ♀", "en", "en-gb"),
    ("bf_lily",    "Lily · English (UK) ♀", "en", "en-gb"),
    ("bm_george",  "George · English (UK) ♂", "en", "en-gb"),
    ("bm_fable",   "Fable · English (UK) ♂", "en", "en-gb"),
    ("bm_lewis",   "Lewis · English (UK) ♂", "en", "en-gb"),
    ("bm_daniel",  "Daniel · English (UK) ♂", "en", "en-gb"),
    ("ef_dora",    "Dora · Spanish ♀", "es", "es"),
    ("em_alex",    "Alex · Spanish ♂", "es", "es"),
    ("ff_siwis",   "Siwis · French ♀", "fr", "fr-fr"),
    ("if_sara",    "Sara · Italian ♀", "it", "it"),
    ("im_nicola",  "Nicola · Italian ♂", "it", "it"),
    ("pf_dora",    "Dora · Portuguese ♀", "pt", "pt-br"),
    ("pm_alex",    "Alex · Portuguese ♂", "pt", "pt-br"),
    ("hf_alpha",   "Alpha · Hindi ♀", "hi", "hi"),
    ("hm_omega",   "Omega · Hindi ♂", "hi", "hi"),
    ("jf_alpha",   "Alpha · Japanese ♀", "ja", "ja"),
    ("jm_kumo",    "Kumo · Japanese ♂", "ja", "ja"),
    ("zf_xiaoxiao", "Xiaoxiao · Chinese ♀", "zh", "cmn"),
    ("zm_yunxi",   "Yunxi · Chinese ♂", "zh", "cmn"),
]

# Piper voices: whatever .onnx files sit in ~/.local/share/voxbox/voices, one
# per language for everything Kokoro lacks. Add more with
#   cd ~/.local/share/voxbox/voices && ../venv/bin/python -m piper.download_voices <name>
# (names: https://huggingface.co/rhasspy/piper-voices/blob/main/voices.json)
PIPER_GENDER = {"el_GR-joy-medium": "♀", "el_GR-rapunzelina-medium": "♀"}


def discover_piper_voices():
    """[(id, label, iso, None)] for every voice in VOICES_DIR, sorted by language."""
    found = []
    for meta in sorted(VOICES_DIR.glob("*.onnx.json")):
        vid = meta.name[: -len(".onnx.json")]
        if not (VOICES_DIR / f"{vid}.onnx").exists():
            continue
        try:
            info = json.loads(meta.read_text())
            code = info["language"]["code"]
            language = info["language"].get("name_english") or code
            dataset = info.get("dataset") or vid.split("-")[1]
        except Exception:
            continue
        iso = code.split("_")[0].lower()
        name = dataset.replace("_", " ").title()
        region = code.split("_")[1] if "_" in code else ""
        label = f"{name} · {language}"
        if iso in ("en", "es", "pt", "nl") and region:
            label += f" ({region})"
        if vid in PIPER_GENDER:
            label += f" {PIPER_GENDER[vid]}"
        found.append((vid, label, iso, None, language))
    found.sort(key=lambda v: (v[4], v[1]))
    return [(vid, label, iso, lang) for vid, label, iso, lang, _ in found]


def build_voice_list():
    """[(id, label, iso, engine, engine_lang)] in dropdown order: Kokoro, then piper by language."""
    out = [(vid, lbl, iso, "kokoro", klang) for vid, lbl, iso, klang in KOKORO_VOICES]
    out += [(vid, lbl, iso, "piper", None) for vid, lbl, iso, _ in discover_piper_voices()]
    return out


VOICES = build_voice_list()
VOICE_INDEX = {v[0]: i for i, v in enumerate(VOICES)}
VOICE_INFO = {vid: {"label": lbl, "iso": iso, "engine": eng, "engine_lang": elang} for vid, lbl, iso, eng, elang in VOICES}
AVAILABLE_LANGS = sorted({v[2] for v in VOICES})
# First voice listed for a language is its default - Kokoro entries come first,
# so they win over piper where both exist.
DEFAULT_VOICE_FOR_LANG = {}
for _vid, _lbl, _iso, _eng, _elang in VOICES:
    DEFAULT_VOICE_FOR_LANG.setdefault(_iso, _vid)


def voice_iso(vid):
    return VOICE_INFO.get(vid, {}).get("iso", "en")


# -- language detection ---------------------------------------------------
#
# Two stages: the script (alphabet) narrows the candidates for free, then
# lingua decides between languages that share a script. Short fragments stick
# with the previous sentence's language rather than guessing.

_SCRIPT_RANGES = {
    "greek": [(0x0370, 0x03FF), (0x1F00, 0x1FFF)],
    "cyrillic": [(0x0400, 0x04FF), (0x0500, 0x052F)],
    "arabic": [(0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)],
    "hebrew": [(0x0590, 0x05FF)],
    "armenian": [(0x0530, 0x058F)],
    "georgian": [(0x10A0, 0x10FF)],
    "devanagari": [(0x0900, 0x097F)],
    "bengali": [(0x0980, 0x09FF)],
    "telugu": [(0x0C00, 0x0C7F)],
    "malayalam": [(0x0D00, 0x0D7F)],
    "hangul": [(0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F)],
    "kana": [(0x3040, 0x30FF)],
    "han": [(0x4E00, 0x9FFF), (0x3400, 0x4DBF)],
    "latin": [(0x0041, 0x005A), (0x0061, 0x007A), (0x00C0, 0x024F), (0x1E00, 0x1EFF)],
}
SCRIPT_LANGS = {
    "greek": ["el"], "cyrillic": ["ru", "uk", "bg", "sr", "kk"], "arabic": ["ar", "fa", "ur"],
    "hebrew": ["he"], "armenian": ["hy"], "georgian": ["ka"], "devanagari": ["hi", "mr", "ne"],
    "bengali": ["bn"], "telugu": ["te"], "malayalam": ["ml"], "hangul": ["ko"], "kana": ["ja"],
    "han": ["zh", "ja"],
}


def script_of(text):
    """The alphabet a sentence is mostly written in ('latin', 'greek', ...)."""
    counts = {}
    for ch in text:
        o = ord(ch)
        for script, ranges in _SCRIPT_RANGES.items():
            if any(lo <= o <= hi for lo, hi in ranges):
                counts[script] = counts.get(script, 0) + 1
                break
    if not counts:
        return "latin"
    if counts.get("kana"):          # any kana at all means Japanese, not Chinese
        counts["han"] = 0
    return max(counts, key=counts.get)


def _letter_count(text):
    return sum(1 for ch in text if ch.isalpha())


class LanguageRouter:
    """Picks an ISO 639-1 code per sentence, limited to languages we have a voice for."""

    MIN_LETTERS = 12      # below this a fragment inherits the previous language
    # lingua is sure about full sentences (~1.0) but guesses on short ones
    # ("Hello Marcus." → Italian), so short text must be near-certain
    # before it can pull the reading away from the current language.
    CONFIDENCE_LONG = 0.5
    CONFIDENCE_SHORT = 0.8
    LONG_LETTERS = 40

    def __init__(self):
        self._detectors = {}
        self._lock = threading.Lock()
        self._lingua = None

    def _candidates(self, script):
        if script == "latin":
            non_latin = {l for langs in SCRIPT_LANGS.values() for l in langs}
            return [l for l in AVAILABLE_LANGS if l not in non_latin]
        return [l for l in SCRIPT_LANGS.get(script, []) if l in AVAILABLE_LANGS]

    def _detector(self, script, candidates):
        with self._lock:
            if script in self._detectors:
                return self._detectors[script]
            det = None
            try:
                from lingua import Language, LanguageDetectorBuilder

                by_iso = {}
                for lang in Language.all():
                    iso = lang.iso_code_639_1.name.lower()
                    by_iso.setdefault({"nb": "no", "nn": "no"}.get(iso, iso), []).append(lang)
                langs = [l for c in candidates for l in by_iso.get(c, [])]
                if len({l for l in langs}) >= 2:
                    det = LanguageDetectorBuilder.from_languages(*langs).with_low_accuracy_mode().build()
            except Exception:
                det = None
            self._detectors[script] = det
            return det

    def detect(self, text, previous=None, preferred=None):
        """`previous` is the last sentence's language, `preferred` the selected
        voice's; both outrank a weak guess."""
        script = script_of(text)
        candidates = self._candidates(script)
        if not candidates:
            return previous
        if len(candidates) == 1:
            return candidates[0]
        fallback = next((l for l in (previous, preferred, "en", candidates[0]) if l in candidates), None)
        letters = _letter_count(text)
        if letters < self.MIN_LETTERS:
            return fallback
        threshold = self.CONFIDENCE_LONG if letters >= self.LONG_LETTERS else self.CONFIDENCE_SHORT
        det = self._detector(script, candidates)
        if det is None:
            return fallback
        try:
            ranked = det.compute_language_confidence_values(text)
        except Exception:
            ranked = []
        for entry in ranked:
            iso = entry.language.iso_code_639_1.name.lower()
            iso = {"nb": "no", "nn": "no"}.get(iso, iso)
            if iso not in candidates:
                continue
            if iso in (previous, preferred) and entry.value >= 0.3:
                return iso          # staying put needs less proof than switching
            return fallback if entry.value < threshold else iso
        return fallback


ROUTER = LanguageRouter()


# --------------------------------------------------------------------------- text


def clean_text(text):
    """Undo the line-wrapping artefacts OCR and PDFs leave behind."""
    text = text.replace("\r", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"-\n(?=\w)", "", text)          # re-join hyphenated breaks
    text = re.sub(r"(?<=\S)\n(?=\S)", " ", text)   # unwrap soft line breaks
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


# Words that end in a period without ending a sentence, so the next chunk is
# folded back onto them. Greek and English, plus the "e.g"/"i.e" forms.
_ABBREV = {
    "dr", "mr", "mrs", "ms", "prof", "sr", "jr", "st", "vs", "etc", "fig",  # "no." omitted: a "No." sentence beats "No. 5"
    "vol", "al", "inc", "ltd", "co", "e.g", "i.e", "u.s", "a.m", "p.m", "approx",  # noqa: E501
    "δρ", "κ", "κα", "κος", "π.χ", "κλπ", "βλ", "σελ",
}
_INITIALISM = re.compile(r"(?:\w\.){1,}\w?$", re.UNICODE)  # A.E.  U.S.  e.g.  J.R.R.


def split_sentences(text):
    """Split into speakable chunks at sentence boundaries.

    Greek uses ';' and '·' as terminal marks, so those split too. A chunk is
    folded back onto the previous one only when the previous ends in an
    abbreviation, or the chunk is a stray single character - never when both
    are real sentences.
    """
    parts = re.split(r"(?<=[.!?…;·\u037e\u0387])\s+|\n+", text)
    out = []
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        prev = out[-1] if out else ""
        prev_tail = re.findall(r"\S+", prev)[-1] if prev else ""
        last_word = prev_tail.rstrip(".").lower()
        stray = len(re.sub(r"[^\w]", "", p, flags=re.UNICODE)) <= 1
        merge = (
            bool(out)
            and len(prev) < 240
            and script_of(p) == script_of(prev)
            and (last_word in _ABBREV or _INITIALISM.match(prev_tail) or stray)
        )
        if merge:
            out[-1] = prev + " " + p
        else:
            out.append(p)
    # Hard bounds: no chunk longer than the sentence ceiling, and no more
    # chunks than the list ceiling.
    bounded = []
    for chunk in out:
        while len(chunk) > MAX_SENTENCE_CHARS and len(bounded) < MAX_SENTENCES:
            cut = chunk.rfind(" ", MAX_SENTENCE_CHARS // 2, MAX_SENTENCE_CHARS)
            if cut < 0:
                cut = MAX_SENTENCE_CHARS
            bounded.append(chunk[:cut])
            chunk = chunk[cut:].lstrip()
        if len(bounded) >= MAX_SENTENCES:
            break
        bounded.append(chunk)
    out = bounded
    return out or ([text] if text.strip() else [])


# --------------------------------------------------------------------------- engines


def _tame_onnxruntime():
    """Cap each ONNX session's thread pool. Every loaded voice otherwise spawns
    a pool sized to all 20 cores; concurrent synthesis then oversubscribes the
    CPU so badly that the audio callback starves and playback stutters.
    Synthesis stays far above realtime with a few threads."""
    import onnxruntime as ort

    if getattr(ort, "_voxbox_tamed", False):
        return
    real = ort.InferenceSession

    def tamed(*args, **kwargs):
        if kwargs.get("sess_options") is None:
            options = ort.SessionOptions()
            options.intra_op_num_threads = max(2, (os.cpu_count() or 8) // 4)
            options.inter_op_num_threads = 1
            kwargs["sess_options"] = options
        return real(*args, **kwargs)

    ort.InferenceSession = tamed
    ort._voxbox_tamed = True


class _CoercingSession:
    """kokoro-onnx 0.4.7 feeds `speed` as int32; this model wants float32.

    Rather than patch site-packages (an upgrade would silently undo it), cast
    every input to whatever dtype the loaded model actually declares.
    """

    _TYPES = {
        "tensor(float)": np.float32,
        "tensor(int64)": np.int64,
        "tensor(int32)": np.int32,
    }

    def __init__(self, sess):
        self._sess = sess
        self._want = {i.name: i.type for i in sess.get_inputs()}
        self.speed_override = None   # set by KokoroEngine per synthesis

    def __getattr__(self, name):
        return getattr(self._sess, name)

    def run(self, output_names, feed, *args, **kwargs):
        fixed = {}
        for key, value in feed.items():
            arr = np.asarray(value)
            if key == "speed" and self.speed_override is not None:
                # Undo kokoro-onnx's np.array([speed], dtype=int32) truncation.
                arr = np.array([self.speed_override], dtype=np.float32)
            want = self._TYPES.get(self._want.get(key))
            fixed[key] = arr.astype(want) if want is not None and arr.dtype != want else arr
        return self._sess.run(output_names, fixed, *args, **kwargs)


class KokoroEngine:
    """Offline neural TTS. ~0.7s to load, then ~6x realtime on CPU."""

    name = "kokoro"

    def __init__(self):
        self._k = None
        self._lock = threading.Lock()

    def _model(self):
        with self._lock:
            if self._k is None:
                _tame_onnxruntime()   # before kokoro_onnx binds InferenceSession
                from kokoro_onnx import Kokoro

                k = Kokoro(str(MODELS / "kokoro-v1.0.onnx"), str(MODELS / "voices-v1.0.bin"))
                k.sess = _CoercingSession(k.sess)
                self._k = k
            return self._k

    def synth(self, text, voice, speed, lang):
        model = self._model()
        # speed_override is shared mutable state on the session, so one Kokoro
        # synthesis at a time. Synthesis is fast; the lookahead still stays warm.
        with self._lock:
            model.sess.speed_override = float(speed)
            try:
                samples, rate = model.create(text, voice=voice, speed=speed, lang=lang or "en-us")
            finally:
                model.sess.speed_override = None
        samples = np.asarray(samples, dtype=np.float32)
        return samples if rate == SR else resample(samples, rate, SR)


class PiperEngine:
    """Offline VITS voices (piper) for the languages Kokoro lacks.

    ~40x realtime on CPU; each voice loads on first use and stays cached.
    """

    name = "piper"

    def __init__(self):
        self._voices = {}
        self._lock = threading.Lock()

    def _voice(self, voice_id):
        with self._lock:
            voice = self._voices.get(voice_id)
            if voice is None:
                _tame_onnxruntime()   # before piper binds InferenceSession
                from piper import PiperVoice

                voice = PiperVoice.load(str(VOICES_DIR / f"{voice_id}.onnx"))
                self._voices[voice_id] = voice
            return voice

    def synth(self, text, voice, speed, lang=None):
        from piper.config import SynthesisConfig

        # piper normalises to full scale; 0.45 lands it at Kokoro's loudness.
        cfg = SynthesisConfig(length_scale=1.0 / max(speed, 0.1), volume=0.45)
        chunks = list(self._voice(voice).synthesize(text, cfg))
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        samples = np.concatenate([np.asarray(c.audio_float_array, dtype=np.float32) for c in chunks])
        return resample(samples, chunks[0].sample_rate, SR)


def resample(samples, src, dst):
    if src == dst or len(samples) == 0:
        return samples
    n = int(round(len(samples) * dst / src))
    return np.interp(
        np.linspace(0, len(samples) - 1, n, dtype=np.float64),
        np.arange(len(samples), dtype=np.float64),
        samples,
    ).astype(np.float32)


ENGINES = {"kokoro": KokoroEngine(), "piper": PiperEngine()}


def route_voice(sentence, base_voice, lang_voices, previous=None):
    """Which voice reads this sentence: the base voice, unless auto-detect finds
    another language, then that language's chosen (or default) voice.
    Returns (voice_id, iso). Shared by live playback and file export so both
    route identically."""
    base_iso = voice_iso(base_voice)
    lang = ROUTER.detect(sentence, previous, base_iso) or base_iso
    if lang == base_iso:
        return base_voice, lang
    return (lang_voices.get(lang) or DEFAULT_VOICE_FOR_LANG.get(lang) or base_voice), lang


def synthesize_all(text, base_voice, lang_voices, speed, on_progress=None):
    """Render the whole text to one mono float array, routing each sentence by
    language exactly as playback does. A short gap separates sentences."""
    sentences = split_sentences(text)
    gap = np.zeros(int(SR * 0.25), dtype=np.float32)
    chunks = []
    previous = None
    for i, sentence in enumerate(sentences):
        voice, previous = route_voice(sentence, base_voice, lang_voices, previous)
        info = VOICE_INFO.get(voice, {"engine": "kokoro", "engine_lang": "en-us"})
        try:
            audio = ENGINES[info["engine"]].synth(sentence, voice, speed, info["engine_lang"])
        except Exception:
            audio = np.zeros(int(SR * 0.15), dtype=np.float32)
        chunks.append(np.asarray(audio, dtype=np.float32))
        chunks.append(gap)
        if on_progress:
            on_progress(i + 1, len(sentences))
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


def encode_audio(samples, path, volume=1.0):
    """Write mono float samples to path; ffmpeg picks the codec from the
    extension (.mp3 -> libmp3lame, .wav/.flac/.ogg -> matching encoder)."""
    pcm = np.clip(np.asarray(samples, dtype=np.float32) * volume, -1.0, 1.0).tobytes()
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "pipe:0", str(path)],
        input=pcm, check=True,
    )


# --------------------------------------------------------------------------- player


class Player:
    """Sentence-at-a-time playback with a synthesis thread running ahead.

    Audio is produced one sentence ahead of the speakers so playback starts
    almost immediately and skipping between sentences stays instant.
    """

    LOOKAHEAD = 6

    def __init__(self, on_sentence, on_state, on_error):
        self._lock = threading.RLock()
        self._sentences = []
        self._audio = {}
        self._idx = 0
        self._pos = 0
        self._playing = False
        self._gen = 0
        self._stream = None
        self._worker = None
        self._preview = None
        self._preview_pos = 0
        self.volume = 0.85
        self.speed = 1.0
        self.voice = DEFAULTS["voice"]
        self.auto_language = True
        self.lang_voices = {}          # iso -> voice the user chose for that language
        self._langs = {}               # sentence index -> detected iso
        self.on_sentence = on_sentence
        self.on_state = on_state
        self.on_error = on_error

    # -- stream -------------------------------------------------------------

    def _ensure_stream(self):
        if self._stream is None:
            # Fixed blocksize: on PortAudio-over-PipeWire, blocksize=0 (the
            # sd.play default) stutters badly whenever synthesis is running -
            # measured 70 dropouts in a 3s clip vs 0 with a fixed size.
            self._stream = sd.OutputStream(
                samplerate=SR, channels=1, dtype="float32",
                blocksize=1024, latency="high", callback=self._callback,
            )
            self._stream.start()

    def _callback(self, outdata, frames, time_info, status):  # audio thread
        with self._lock:
            if self._preview is not None:
                buf, pos = self._preview, self._preview_pos
                take = min(frames, len(buf) - pos)
                if take > 0:
                    outdata[:take, 0] = buf[pos:pos + take] * self.volume
                outdata[take:] = 0
                self._preview_pos += take
                if self._preview_pos >= len(buf):
                    self._preview = None
                return
            if not self._playing:
                outdata.fill(0)
                return
            written = 0
            while written < frames:
                buf = self._audio.get(self._idx)
                if buf is None:                      # synthesis hasn't caught up
                    outdata[written:] = 0
                    return
                take = min(frames - written, len(buf) - self._pos)
                if take > 0:
                    outdata[written:written + take, 0] = buf[self._pos:self._pos + take] * self.volume
                    written += take
                    self._pos += take
                if self._pos >= len(buf):
                    self._idx += 1
                    self._pos = 0
                    if self._idx >= len(self._sentences):
                        outdata[written:] = 0
                        self._playing = False
                        GLib.idle_add(self.on_state, False, True)
                        return
                    GLib.idle_add(self.on_sentence, self._idx)
            return

    # -- synthesis ----------------------------------------------------------

    def _spawn_worker(self):
        gen = self._gen
        self._worker = threading.Thread(target=self._synth_loop, args=(gen,), daemon=True)
        self._worker.start()

    def voice_for(self, index, sentence):
        """The selected voice, unless auto-detect says the sentence is in another language."""
        if not self.auto_language:
            self._langs[index] = voice_iso(self.voice)
            return self.voice
        previous = self._langs.get(index - 1)
        voice, lang = route_voice(sentence, self.voice, self.lang_voices, previous)
        self._langs[index] = lang
        return voice

    def lang_of(self, index):
        return self._langs.get(index)

    def _synth_loop(self, gen):
        while True:
            with self._lock:
                if gen != self._gen:
                    return
                target = None
                for i in range(self._idx, min(self._idx + self.LOOKAHEAD, len(self._sentences))):
                    if i not in self._audio:
                        target = i
                        break
                if target is None:
                    done = all(i in self._audio for i in range(len(self._sentences)))
                    if done:
                        return
                sentence = self._sentences[target] if target is not None else None
                speed = self.speed
            if target is None:
                time.sleep(0.05)
                continue
            voice = self.voice_for(target, sentence)   # outside the lock: may load a detector
            info = VOICE_INFO.get(voice, {"engine": "kokoro", "engine_lang": "en-us"})
            engine_name, lang = info["engine"], info["engine_lang"]
            try:
                samples = ENGINES[engine_name].synth(sentence, voice, speed, lang)
            except Exception as exc:  # keep going; one bad sentence shouldn't stall
                GLib.idle_add(self.on_error, f"{engine_name}: {exc}")
                samples = np.zeros(int(SR * 0.2), dtype=np.float32)
            with self._lock:
                if gen != self._gen:
                    return
                self._audio[target] = samples

    # -- controls -----------------------------------------------------------

    def load(self, sentences):
        with self._lock:
            self._gen += 1
            self._sentences = list(sentences)
            self._audio.clear()
            self._langs.clear()
            self._idx = 0
            self._pos = 0
            self._playing = False
        self._spawn_worker()
        self.on_sentence(0)

    def restart_from(self, idx, keep_playing=True):
        """Re-synthesise from `idx` on - used when voice or speed changes."""
        with self._lock:
            if not self._sentences:
                return
            was = self._playing
            self._playing = False
            self._gen += 1
            self._idx = max(0, min(idx, len(self._sentences) - 1))
            self._pos = 0
            # Drop every cached clip: the ones already rendered used the old
            # voice/speed, so rewinding into them would sound wrong.
            self._audio.clear()
            self._langs = {i: l for i, l in self._langs.items() if i < self._idx}
        self._spawn_worker()
        self.on_sentence(self._idx)
        if was and keep_playing:
            self.play()

    def preview(self, samples):
        """Play a one-off clip through the same stream, pausing sentence playback."""
        with self._lock:
            self._playing = False
            self._preview = np.asarray(samples, dtype=np.float32)
            self._preview_pos = 0
        self._ensure_stream()
        self.on_state(False, False)

    def play(self):
        with self._lock:
            self._preview = None
            if not self._sentences:
                return
            if self._idx >= len(self._sentences):
                self._idx, self._pos = 0, 0
            self._playing = True
        self._ensure_stream()
        self.on_state(True, False)

    def pause(self):
        with self._lock:
            self._playing = False
        self.on_state(False, False)

    def toggle(self):
        self.pause() if self._playing else self.play()

    def stop(self):
        with self._lock:
            self._preview = None
            self._playing = False
            self._idx = 0
            self._pos = 0
        self.on_state(False, True)
        self.on_sentence(0)

    def jump(self, delta):
        with self._lock:
            if not self._sentences:
                return
            new = max(0, min(self._idx + delta, len(self._sentences) - 1))
            self._idx = new
            self._pos = 0
        self.on_sentence(new)

    def goto(self, idx):
        with self._lock:
            if not self._sentences:
                return
            self._idx = max(0, min(idx, len(self._sentences) - 1))
            self._pos = 0
        self.on_sentence(self._idx)

    @property
    def playing(self):
        return self._playing

    @property
    def index(self):
        return self._idx

    @property
    def count(self):
        return len(self._sentences)


# --------------------------------------------------------------------------- capture


def _open_bounded(path, limit):
    """Open a document exactly once, race-safe: the final path component must
    not be a symlink, the opened descriptor must be a regular file, and its
    size (checked on that same descriptor) must be within the limit. All
    subsequent reads use this descriptor, so a swap after the check is inert."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise InputTooLarge("symlinked documents are not followed")
        raise
    try:
        st = os.fstat(fd)
        if not stat_module.S_ISREG(st.st_mode):
            raise InputTooLarge("not a regular file")
        if st.st_size > limit:
            raise InputTooLarge(f"file larger than {limit // (1024 * 1024)} MB")
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def _read_member(zf, name, limit):
    """Stream a ZIP member reading at most limit+1 decompressed bytes - never
    trusts the header's claimed size."""
    with zf.open(name) as member:
        data = member.read(limit + 1)
    if len(data) > limit:
        raise InputTooLarge("EPUB member too large")
    return data


def _strip_html(html):
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<(br|/p|/div|/h[1-6]|/li)[^>]*>", "\n", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    import html as _html
    return _html.unescape(html)


def _epub_text(fileobj):
    """Extract an EPUB's text in reading order (OPF spine), tags stripped.

    Every member - the OPF manifest included - is streamed under a shared
    decompressed-byte budget, and the archive's entry count is capped, so a
    zip bomb fails closed before it can allocate anything meaningful.
    """
    import zipfile

    budget = MAX_FILE_BYTES
    with zipfile.ZipFile(fileobj) as zf:
        names = zf.namelist()
        if len(names) > MAX_EPUB_MEMBERS:
            raise InputTooLarge("EPUB has too many entries")
        opf = next((n for n in names if n.lower().endswith(".opf")), None)
        order = []
        if opf:
            raw = _read_member(zf, opf, min(budget, 4 * 1024 * 1024))
            budget -= len(raw)
            manifest = raw.decode("utf-8", "replace")
            base = opf.rsplit("/", 1)[0] if "/" in opf else ""
            hrefs = dict(re.findall(r'<item[^>]*id="([^"]+)"[^>]*href="([^"]+)"', manifest))
            hrefs.update({i: h for h, i in re.findall(r'<item[^>]*href="([^"]+)"[^>]*id="([^"]+)"', manifest)})
            for idref in re.findall(r'<itemref[^>]*idref="([^"]+)"', manifest):
                href = hrefs.get(idref)
                if href:
                    order.append(f"{base}/{href}" if base else href)
        if not order:
            order = [n for n in names if n.lower().endswith((".xhtml", ".html", ".htm"))]
        parts = []
        for name in order:
            try:
                raw = _read_member(zf, name, budget)
            except KeyError:
                continue
            budget -= len(raw)
            parts.append(_strip_html(raw.decode("utf-8", "replace")))
            if budget <= 0:
                raise InputTooLarge("EPUB content too large")
    return "\n\n".join(parts)


def open_document(path):
    """Read a document to plain text. Supports txt/md, PDF (pdftotext), EPUB, HTML."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".pdf":
        # Probe once (rejects FIFOs, symlinks, oversized files), then let
        # pdftotext run; a swap after the probe is bounded anyway by
        # run_capped's output ceiling and timeout.
        _open_bounded(path, MAX_FILE_BYTES).close()
        out = run_capped(
            ["pdftotext", "-layout", "-nopgbrk", str(path), "-"],
            MAX_TEXT_CHARS * 6, timeout=120,
        )
        return out.decode("utf-8", "replace")
    with _open_bounded(path, MAX_FILE_BYTES) as handle:
        if ext == ".epub":
            return _epub_text(handle)
        raw = handle.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise InputTooLarge(f"file larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    text = raw.decode("utf-8", "replace")
    if ext in (".html", ".htm", ".xhtml"):
        return _strip_html(text)
    return text


# tesseract's orientation-and-script detection names the alphabet of a crop;
# that picks the OCR language pack, so Greek on screen is read with the Greek
# model instead of being forced through the English one (which turns it into
# Latin-lookalike garbage that then detects as some random language).
SCRIPT_TESS_LANGS = {
    "Greek": ["ell", "eng"], "Cyrillic": ["rus", "ukr", "bul", "srp", "eng"], "Arabic": ["ara"],
    "Hebrew": ["heb"], "Japanese": ["jpn", "eng"], "Han": ["chi_sim", "eng"], "Hangul": ["kor", "eng"],
    "Devanagari": ["hin", "eng"], "Armenian": ["hye"], "Georgian": ["kat"],
}


# ISO 639-1 (what the voices use) -> tesseract language codes.
ISO_TESS = {
    "en": "eng", "el": "ell", "de": "deu", "fr": "fra", "es": "spa", "it": "ita", "pt": "por", "nl": "nld",
    "ru": "rus", "uk": "ukr", "bg": "bul", "sr": "srp", "pl": "pol", "cs": "ces", "sk": "slk", "hu": "hun",
    "ro": "ron", "tr": "tur", "sv": "swe", "da": "dan", "no": "nor", "fi": "fin", "ar": "ara", "he": "heb",
    "ja": "jpn", "zh": "chi_sim", "ko": "kor", "hi": "hin", "ca": "cat", "eu": "eus", "is": "isl", "et": "est",
    "lv": "lav", "sl": "slv", "sq": "sqi", "vi": "vie", "id": "ind", "hy": "hye", "ka": "kat",
}


def _tessdata_args():
    return ["--tessdata-dir", str(TESSDATA_DIR)] if (TESSDATA_DIR / "eng.traineddata").exists() else []


def _available_tess_langs():
    if (TESSDATA_DIR / "eng.traineddata").exists():
        return {p.name[:-len(".traineddata")] for p in TESSDATA_DIR.glob("*.traineddata")}
    try:
        out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=5).stdout
        return {line.strip() for line in out.splitlines()[1:] if line.strip()}
    except Exception:
        return {"eng"}


def detect_script(image_png):
    """'Greek', 'Latin', 'Cyrillic', ... via `tesseract --psm 0`, or None."""
    try:
        out = subprocess.run(
            ["tesseract", "stdin", "stdout", "--psm", "0", *_tessdata_args()],
            input=image_png, capture_output=True, timeout=10,
        ).stdout.decode("utf-8", "replace")
    except Exception:
        return None
    m = re.search(r"^Script:\s*(\S+)", out, re.M)
    return m.group(1) if m else None


def ocr_image(image_png, base_langs="eng", user_isos=()):
    """OCR a PNG crop; returns (text, tesseract language string used).

    The language pack is the union of: the alphabet tesseract's script
    detector sees in the crop, the languages of the voices the user actually
    uses, and the configured base. Script detection alone is not enough - it
    calls a mostly-Greek crop with a few English words "Latin" - and the union
    keeps mixed text readable either way.
    """
    available = _available_tess_langs()
    script = detect_script(image_png)
    pack = [l for l in SCRIPT_TESS_LANGS.get(script or "", []) if l in available]
    mine = [ISO_TESS[i] for i in user_isos if ISO_TESS.get(i) in available]
    base = [l for l in re.split(r"[+,\s]+", base_langs) if l in available]
    langs = list(dict.fromkeys(pack + mine + base)) or ["eng"]
    lang_arg = "+".join(langs)
    ocr_out = run_capped(
        ["tesseract", "stdin", "stdout", "--oem", "1", "--psm", "6", "-l", lang_arg, "--dpi", "300",
         "-c", "preserve_interword_spaces=1", *_tessdata_args()],
        MAX_OCR_BYTES, input_bytes=image_png, timeout=60,
    )
    return clean_text(ocr_out.decode("utf-8", "replace")), lang_arg


_capture_lock = threading.Lock()
_slurp_proc = None


def _wait_for_layer(namespace, timeout=1.5):
    """Poll hyprctl until a layer with this namespace is mapped. Returns ms waited, or -1."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            layers = json.loads(subprocess.run(
                ["hyprctl", "layers", "-j"], capture_output=True, text=True, timeout=2
            ).stdout)
            for monitor in layers.values():
                for level in monitor.get("levels", {}).values():
                    for layer in level:
                        if namespace in layer.get("namespace", ""):
                            return int((time.time() - start) * 1000)
        except Exception:
            pass
        time.sleep(0.03)
    return -1


def capture_region(ocr_langs="eng", user_isos=()):
    """Freeze the screen, let the user drag a box, OCR whatever is inside it.

    Serialized: a stuck earlier selection is killed rather than fought - two
    overlapping slurp overlays mean neither can be dragged.
    """
    global _slurp_proc
    if not _capture_lock.acquire(blocking=False):
        if _slurp_proc and _slurp_proc.poll() is None:
            _slurp_proc.kill()
        return None
    freeze = None
    try:
        if which("hyprpicker"):
            freeze = subprocess.Popen(
                ["hyprpicker", "-r", "-z"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            # Wait until the freeze layer is actually mapped before starting
            # slurp. slurp must map second so it stacks on top - otherwise the
            # picker eats the first click (prints a color, exits, unfreezes)
            # and the drag never reaches slurp.
            _wait_for_layer("hyprpicker", timeout=1.5)
            time.sleep(0.05)
        _slurp_proc = subprocess.Popen(["slurp"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            sel = (_slurp_proc.communicate(timeout=120)[0] or "").strip()
        except subprocess.TimeoutExpired:
            _slurp_proc.kill()
            sel = ""
        if not sel:
            return None
        shot = run_capped(["grim", "-g", sel, "-"], MAX_IMAGE_BYTES, timeout=30)
    finally:
        if freeze:
            freeze.terminate()
            try:
                freeze.wait(timeout=2)   # reap; no zombies
            except subprocess.TimeoutExpired:
                freeze.kill()
        _capture_lock.release()
    if not shot:
        return None
    text, _langs = ocr_image(shot, ocr_langs, user_isos)
    return text


def capture_selection():
    for args in (["wl-paste", "--primary", "--no-newline"], ["wl-paste", "--no-newline"]):
        try:
            raw = run_capped(args, MAX_TEXT_CHARS * 4, timeout=5)
        except InputTooLarge:
            raise InputTooLarge("selection too large to read")
        except Exception:
            continue
        text = raw.decode("utf-8", "replace")
        if text and text.strip():
            return clean_text(text)
    return None


def notify(message):
    if which("omarchy-notification-send"):
        subprocess.Popen(["omarchy-notification-send", "-g", "\U000f0507", message],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --------------------------------------------------------------------------- window

# Nerd Font glyphs (Material Design set), the same family the shell's panels use.
GLYPH = {
    "hero": "\U000F050A", "region": "\U000F0D11", "selection": "\U000F0F4F",
    "prev": "\U000F04AE", "play": "\U000F040A", "pause": "\U000F03E4", "next": "\U000F04AD",
    "stop": "\U000F04DB", "close": "\U000F0156", "voice": "\U000F0033",
    "speed": "\U000F04C5", "volume": "\U000F057E",
    "open": "\U000F0770", "export": "\U000F0224",
    "pin": "\U000F0403", "unpin": "\U000F0404",
}

PLUGIN_ID = "io.github.nousd.voxbox"


def plugin_pin_state():
    """None if the Omarchy shell plugin isn't available, else True/False for enabled."""
    if not which("omarchy"):
        return None
    try:
        out = subprocess.run(["omarchy", "plugin", "list", "--json"],
                             capture_output=True, text=True, timeout=10).stdout
        for plugin in json.loads(out):
            if plugin.get("id") == PLUGIN_ID:
                return bool(plugin.get("enabled"))
    except Exception:
        pass
    return None


def plugin_set_pinned(pinned):
    subprocess.run(["omarchy", "plugin", "enable" if pinned else "disable", PLUGIN_ID],
                   capture_output=True, timeout=15)


def glyph_label(name, css="icon"):
    label = Gtk.Label(label=GLYPH[name])
    label.add_css_class(css)
    return label


def icon_button(glyph, text=None, tooltip=None, classes=()):
    """A shell-style button: glyph, optional label, bordered by default."""
    button = Gtk.Button()
    inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, halign=Gtk.Align.CENTER)
    inner.append(glyph_label(glyph))
    if text:
        inner.set_spacing(THEME.tokens.get("space", {}).get("control-gap", 8))
        inner.append(Gtk.Label(label=text))
    button.set_child(inner)
    for cls in ("bordered", *classes):
        button.add_css_class(cls)
    if tooltip:
        button.set_tooltip_text(tooltip)
    return button


class VoxboxWindow(Adw.ApplicationWindow):
    """Laid out like an Omarchy shell panel: hero, separators, caption headers."""

    def __init__(self, app):
        super().__init__(application=app, title="Voxbox")
        self.cfg = load_config()
        self.set_default_size(440, 640)
        self.set_hide_on_close(True)

        self.player = Player(self._on_sentence, self._on_state, self._on_error)
        self.player.volume = float(self.cfg["volume"])
        self.player.speed = float(self.cfg["speed"])
        self.player.voice = self.cfg["voice"] if self.cfg["voice"] in VOICE_INFO else DEFAULTS["voice"]
        self.player.lang_voices = {k: v for k, v in self.cfg.get("voices_by_lang", {}).items() if v in VOICE_INFO}
        self._sentences = []
        self._offsets = []
        self._busy = False
        self._resynth_timer = None
        self._status = "idle"
        self._source_line = "nothing loaded"
        self._blocking_voice = False

        sp = THEME.tokens.get("space", self.__class__.FALLBACK_SPACE)
        self.sp = sp
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=sp["panel-gap"])
        panel.add_css_class("panel")
        self.set_content(panel)

        panel.append(self._build_hero())
        panel.append(self._separator())
        panel.append(self._build_text_section())
        panel.append(self._separator())
        panel.append(self._build_playback_section())
        panel.append(self._separator())
        panel.append(self._build_voice_section())

        self._install_shortcuts()
        self._select_voice(self.cfg["voice"])
        THEME.subscribe(self._on_theme)
        self._on_theme(THEME)
        self._refresh_meta()

    FALLBACK_SPACE = OmarchyTheme.SPACING

    # -- construction -------------------------------------------------------

    def _separator(self):
        sep = Gtk.Box()
        sep.add_css_class("separator")
        return sep

    def _section_header(self, title, value_label=None):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        head = Gtk.Label(label=title.upper(), xalign=0, hexpand=True)
        head.add_css_class("section-header")
        row.append(head)
        if value_label is not None:
            value_label.set_halign(Gtk.Align.END)
            if isinstance(value_label, Gtk.Label):
                value_label.add_css_class("section-value")
                value_label.set_xalign(1)
            row.append(value_label)
        return row

    def _build_hero(self):
        sp = self.sp
        hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=sp["xxxl"])
        hero.append(glyph_label("hero", "hero-icon"))

        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=sp["xxs"], hexpand=True, valign=Gtk.Align.CENTER)
        title = Gtk.Label(label="Voxbox", xalign=0)
        title.add_css_class("hero-title")
        self.meta = Gtk.Label(label="", xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.meta.add_css_class("hero-meta")
        labels.append(title)
        labels.append(self.meta)
        hero.append(labels)

        self.pin_btn = icon_button("pin", tooltip="Pin Voxbox to the bar", classes=("action",))
        self.pin_btn.remove_css_class("bordered")
        self.pin_btn.set_valign(Gtk.Align.CENTER)
        self.pin_btn.set_visible(False)
        self.pin_btn.connect("clicked", lambda *_: self._toggle_pin())
        hero.append(self.pin_btn)
        self._pinned = None
        threading.Thread(target=self._load_pin_state, daemon=True).start()

        close = icon_button("close", tooltip="Hide (Esc)", classes=("action",))
        close.remove_css_class("bordered")
        close.set_valign(Gtk.Align.CENTER)
        close.connect("clicked", lambda *_: self._hide())
        hero.append(close)
        return hero

    def _load_pin_state(self):
        state = plugin_pin_state()
        GLib.idle_add(self._show_pin_state, state)

    def _show_pin_state(self, state):
        self._pinned = state
        self.pin_btn.set_visible(state is not None)
        if state is not None:
            glyph = self.pin_btn.get_child().get_first_child()
            glyph.set_label(GLYPH["unpin"] if state else GLYPH["pin"])
            self.pin_btn.set_tooltip_text(
                "Unpin Voxbox from the bar" if state else "Pin Voxbox to the bar")
        return False

    def _toggle_pin(self):
        if self._pinned is None:
            return
        target = not self._pinned
        self.pin_btn.set_sensitive(False)

        def work():
            try:
                plugin_set_pinned(target)
                state = plugin_pin_state()
            except Exception:
                state = self._pinned
            GLib.idle_add(self._show_pin_state, state)
            GLib.idle_add(self.pin_btn.set_sensitive, True)

        threading.Thread(target=work, daemon=True).start()

    def _build_text_section(self):
        sp = self.sp
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=sp["lg"], vexpand=True)
        self.progress = Gtk.Label(label="")
        self.progress.add_css_class("section-value")
        self.progress.set_valign(Gtk.Align.CENTER)
        head_extra = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=sp["control-gap"])
        head_extra.append(self.progress)
        self.export_btn = icon_button("export", "EXPORT AUDIO",
                                      tooltip="Save this text as an MP3 / WAV / FLAC / OGG file  (Ctrl+E)",
                                      classes=("chip",))
        self.export_btn.connect("clicked", lambda *_: self.do_export())
        head_extra.append(self.export_btn)
        box.append(self._section_header("Text", head_extra))

        self.buffer = Gtk.TextBuffer()
        self.tag_current = self.buffer.create_tag("current", weight=Pango.Weight.BOLD)
        self.view = Gtk.TextView(buffer=self.buffer)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        for side in ("left", "right"):
            getattr(self.view, f"set_{side}_margin")(sp["control-padding-x"])
        self.view.set_top_margin(sp["input-padding-y"] + 2)
        self.view.set_bottom_margin(sp["input-padding-y"] + 2)
        self.view.set_pixels_below_lines(2)
        self.buffer.connect("changed", self._on_text_edited)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self.view)
        scroll.add_css_class("reader")
        box.append(scroll)

        grab = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=sp["control-gap"], homogeneous=True)
        region = icon_button("region", "Region", "Hide the panel, drag a box, read what is inside it  (Ctrl+R)")
        region.connect("clicked", lambda *_: self.do_region())
        selection = icon_button("selection", "Selection", "Read the text you have highlighted  (Ctrl+V)")
        selection.connect("clicked", lambda *_: self.do_selection())
        open_btn = icon_button("open", "Open file", "Read a document: PDF, EPUB, txt, HTML  (Ctrl+O)")
        open_btn.connect("clicked", lambda *_: self.do_open())
        grab.append(region)
        grab.append(selection)
        grab.append(open_btn)
        box.append(grab)
        return box

    def _build_playback_section(self):
        sp = self.sp
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=sp["lg"])
        self.state_label = Gtk.Label(label="")
        box.append(self._section_header("Playback", self.state_label))

        transport = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=sp["control-gap"], halign=Gtk.Align.CENTER)
        prev = icon_button("prev", tooltip="Previous sentence  (←)", classes=("transport",))
        prev.connect("clicked", lambda *_: self.player.jump(-1))
        self.play_btn = icon_button("play", tooltip="Play / pause  (Space)", classes=("transport",))
        self.play_glyph = self.play_btn.get_child().get_first_child()
        self.play_btn.connect("clicked", lambda *_: self.player.toggle())
        nxt = icon_button("next", tooltip="Next sentence  (→)", classes=("transport",))
        nxt.connect("clicked", lambda *_: self.player.jump(1))
        stop = icon_button("stop", tooltip="Stop and rewind", classes=("transport",))
        stop.connect("clicked", lambda *_: self.player.stop())
        for b in (prev, self.play_btn, nxt, stop):
            transport.append(b)
        box.append(transport)
        return box

    def _build_voice_section(self):
        sp = self.sp
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=sp["label-gap"])
        # Sentences are always routed by detected language; the dropdown sets
        # the voice for the language it belongs to.
        self.voice_hint = Gtk.Label(label="")
        box.append(self._section_header("Voice", self.voice_hint))

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=sp["control-gap"])
        self.voice_model = Gtk.StringList()
        for _vid, label, _iso, _eng, _elang in VOICES:
            self.voice_model.append(label)
        self.dropdown = Gtk.DropDown(model=self.voice_model, hexpand=True)
        # Type to filter: 85 voices is too many to scroll.
        self.dropdown.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        self.dropdown.set_enable_search(True)
        # Default search is prefix-match, and every label starts with the voice
        # name - so typing a language ("greek") matched nothing. Substring it.
        self.dropdown.set_search_match_mode(Gtk.StringFilterMatchMode.SUBSTRING)
        self.dropdown.connect("notify::selected", self._on_voice_changed)
        row.append(self.dropdown)
        preview = icon_button("play", tooltip="Hear this voice", classes=("action",))
        preview.connect("clicked", lambda *_: self.preview_voice())
        row.append(preview)
        box.append(row)

        spacer = Gtk.Box()
        spacer.set_size_request(-1, sp["md"])
        box.append(spacer)

        self.speed_label = Gtk.Label(label="")
        box.append(self._section_header("Speed", self.speed_label))
        self.speed_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.5, 2.0, 0.05)
        self.speed_scale.set_value(self.player.speed)
        self.speed_scale.set_draw_value(False)
        self.speed_scale.add_mark(1.0, Gtk.PositionType.BOTTOM, None)
        self.speed_scale.connect("value-changed", self._on_speed_changed)
        box.append(self.speed_scale)
        self.speed_label.set_label(f"{self.player.speed:.2f}×")

        self.vol_label = Gtk.Label(label="")
        box.append(self._section_header("Volume", self.vol_label))
        self.vol_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.01)
        self.vol_scale.set_value(self.player.volume)
        self.vol_scale.set_draw_value(False)
        self.vol_scale.connect("value-changed", self._on_volume_changed)
        box.append(self.vol_scale)
        self.vol_label.set_label(f"{int(round(self.player.volume * 100))}%")
        return box

    def _install_shortcuts(self):
        keys = Gtk.EventControllerKey()

        def on_key(_c, keyval, _code, state):
            focused_in_text = self.get_focus() is self.view
            ctrl = state & Gdk.ModifierType.CONTROL_MASK
            if keyval == Gdk.KEY_Escape:
                self._hide()
                return True
            if ctrl and keyval in (Gdk.KEY_r, Gdk.KEY_R):
                self.do_region()
                return True
            if ctrl and keyval in (Gdk.KEY_v, Gdk.KEY_V):
                self.do_selection()
                return True
            if ctrl and keyval in (Gdk.KEY_o, Gdk.KEY_O):
                self.do_open()
                return True
            if ctrl and keyval in (Gdk.KEY_e, Gdk.KEY_E):
                self.do_export()
                return True
            if focused_in_text:
                return False
            if keyval == Gdk.KEY_space:
                self.player.toggle()
                return True
            if keyval in (Gdk.KEY_Left, Gdk.KEY_h):
                self.player.jump(-1)
                return True
            if keyval in (Gdk.KEY_Right, Gdk.KEY_l):
                self.player.jump(1)
                return True
            return False

        keys.connect("key-pressed", on_key)
        self.add_controller(keys)

    # -- actions ------------------------------------------------------------

    def _hide(self):
        self.player.pause()
        self.set_visible(False)

    def do_region(self):
        if self._busy:
            return
        self._busy = True
        self.player.pause()
        self._set_status("drag a box…")
        self.set_visible(False)

        def work():
            try:
                text = capture_region(self.cfg.get("ocr_langs", "eng"), self._ocr_isos())
            except Exception as exc:
                text, err = None, str(exc)
            else:
                err = None
            GLib.idle_add(self._region_done, text, err)

        # Let the compositor actually drop the window before grim fires.
        GLib.timeout_add(140, lambda: (threading.Thread(target=work, daemon=True).start(), False)[1])

    def _ocr_isos(self):
        """Languages the user reads: the selected voice's first, then every
        language they have picked a voice for."""
        isos = [voice_iso(self.player.voice)]
        isos += [i for i in self.cfg.get("voices_by_lang", {}) if i not in isos]
        return isos[:6]

    def _region_done(self, text, err):
        self._busy = False
        self.present()
        if err:
            self._on_error(err)
        elif text is None:
            self._set_status("cancelled")
        elif not text.strip():
            self._set_status("no text in that box")
        else:
            self.load_text(text, source="region", autoplay=True)
        return False

    def do_selection(self):
        try:
            text = capture_selection()
        except InputTooLarge as exc:
            self._set_status(str(exc))
            return
        if not text:
            self._set_status("nothing selected")
            notify("Voxbox: nothing selected")
            return
        self.load_text(text, source="selection", autoplay=True)

    def do_open(self):
        dialog = Gtk.FileDialog(title="Open a document")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        docs = Gtk.FileFilter()
        docs.set_name("Documents (PDF, EPUB, text, HTML)")
        for pat in ("*.pdf", "*.epub", "*.txt", "*.md", "*.markdown", "*.html", "*.htm", "*.xhtml"):
            docs.add_pattern(pat)
        allf = Gtk.FileFilter()
        allf.set_name("All files")
        allf.add_pattern("*")
        filters.append(docs)
        filters.append(allf)
        dialog.set_filters(filters)
        dialog.set_default_filter(docs)
        dialog.open(self, None, self._open_done)

    def _open_done(self, dialog, result):
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return                          # user cancelled
        path = gfile.get_path()
        if path:
            self._run_open(path)

    def _run_open(self, path):
        self._set_status("reading file…")

        def work():
            try:
                text = open_document(path)
            except Exception as exc:
                GLib.idle_add(self._on_error, f"open failed: {exc}")
                return
            name = Path(path).name
            if text.strip():
                GLib.idle_add(self.load_text, text, name, True)
            else:
                GLib.idle_add(self._set_status, "no readable text in file")

        threading.Thread(target=work, daemon=True).start()

    def do_export(self):
        start, end = self.buffer.get_bounds()
        text = self.buffer.get_text(start, end, False)
        if not text.strip():
            self._set_status("nothing to export")
            return
        dialog = Gtk.FileDialog(title="Export audio")
        dialog.set_initial_name("voxbox.mp3")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        for name, pat in (("MP3 audio", "*.mp3"), ("WAV audio", "*.wav"), ("FLAC audio", "*.flac"), ("OGG audio", "*.ogg")):
            flt = Gtk.FileFilter()
            flt.set_name(name)
            flt.add_pattern(pat)
            filters.append(flt)
        dialog.set_filters(filters)
        dialog.save(self, None, self._export_done)

    def _export_done(self, dialog, result):
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            return                          # cancelled
        path = gfile.get_path()
        if path:
            self._run_export(path)

    def _run_export(self, path):
        if Path(path).suffix == "":
            path += ".mp3"
        start, end = self.buffer.get_bounds()
        text = self.buffer.get_text(start, end, False)
        base = self.player.voice
        lang_voices = dict(self.player.lang_voices)
        speed = self.player.speed
        volume = self.player.volume
        self.export_btn.set_sensitive(False)

        def work():
            try:
                samples = synthesize_all(
                    text, base, lang_voices, speed,
                    on_progress=lambda i, n: GLib.idle_add(self._set_status, f"exporting {i}/{n}…"),
                )
                encode_audio(samples, path, volume)
            except Exception as exc:
                GLib.idle_add(self._on_error, f"export failed: {exc}")
            else:
                name = Path(path).name
                GLib.idle_add(self._set_status, f"saved {name}")
                notify(f"Voxbox: saved {name}")
            finally:
                GLib.idle_add(self.export_btn.set_sensitive, True)

        threading.Thread(target=work, daemon=True).start()

    def load_text(self, text, source="", autoplay=True):
        text = clean_text(text)
        truncated = len(text) > MAX_TEXT_CHARS
        if truncated:
            text = text[:MAX_TEXT_CHARS].rsplit(None, 1)[0]
        self._set_buffer_text(text)
        self._rebuild_sentences(text)
        words = len(text.split())
        self._source_line = f"{words} words from {source}" if source else f"{words} words"
        if truncated:
            self._source_line += " (truncated)"
        self._set_status("ready")
        if autoplay and self._sentences:
            self.player.play()

    def _set_buffer_text(self, text):
        self.buffer.handler_block_by_func(self._on_text_edited)
        self.buffer.set_text(text)
        self.buffer.handler_unblock_by_func(self._on_text_edited)

    def _rebuild_sentences(self, text):
        self._sentences = split_sentences(text)
        # Character offsets so the spoken sentence can be highlighted in place.
        self._offsets = []
        cursor = 0
        for s in self._sentences:
            found = text.find(s, cursor)
            if found < 0:
                found = cursor
            self._offsets.append((found, found + len(s)))
            cursor = found + len(s)
        self.player.load(self._sentences)

    def preview_voice(self):
        self.player.pause()
        idx = self.dropdown.get_selected()
        if idx < 0 or idx >= len(VOICES):
            return
        vid, _lbl, iso, eng, engine_lang = VOICES[idx]
        speed = self.speed_scale.get_value()
        line = PREVIEW_LINES.get(iso, PREVIEW_LINES["en"])

        def work():
            try:
                samples = ENGINES[eng].synth(line, vid, speed, engine_lang)
                # Through the player's own stream: a second PortAudio stream
                # under synthesis load starves and stutters.
                GLib.idle_add(self.player.preview, samples)
            except Exception as exc:
                GLib.idle_add(self._on_error, str(exc))

        threading.Thread(target=work, daemon=True).start()

    # -- state text ---------------------------------------------------------

    def _set_status(self, status):
        self._status = status
        self._refresh_meta()

    def _refresh_meta(self):
        total = len(self._sentences)
        idx = min(self.player.index + 1, total) if total else 0
        lang = self.player.lang_of(self.player.index) if total else None
        lang_part = f" · {lang}" if lang else ""
        self.meta.set_label(f"{self._status}{lang_part} · {self._source_line}".upper())
        self.progress.set_label(f"{idx} / {total}" if total else "")
        self.state_label.set_label(
            ("PLAYING" if self.player.playing else ("PAUSED" if total and idx > 1 else "STOPPED")) if total else ""
        )

    # -- callbacks ----------------------------------------------------------

    def _on_theme(self, theme):
        t = theme.tokens
        if t:
            self.tag_current.set_property("background", t["reading_bg"])
            self.tag_current.set_property("foreground", t["fg"])
        else:
            self.tag_current.set_property("background", "#3584e4")
            self.tag_current.set_property("foreground", "#ffffff")

    def _on_sentence(self, idx):
        start_it = self.buffer.get_start_iter()
        end_it = self.buffer.get_end_iter()
        self.buffer.remove_tag(self.tag_current, start_it, end_it)
        if 0 <= idx < len(self._offsets):
            a, b = self._offsets[idx]
            ia = self.buffer.get_iter_at_offset(a)
            ib = self.buffer.get_iter_at_offset(b)
            self.buffer.apply_tag(self.tag_current, ia, ib)
            self.view.scroll_to_iter(ia, 0.2, False, 0.0, 0.3)
        self._refresh_meta()
        return False

    def _on_state(self, playing, finished):
        self.play_glyph.set_label(GLYPH["pause"] if playing else GLYPH["play"])
        if playing:
            self._status = "reading"
        elif finished:
            self._status = "done" if self._sentences else "idle"
        else:
            self._status = "paused" if self._sentences else "idle"
        self._refresh_meta()
        return False

    def _on_error(self, message):
        self._set_status(message[:60])
        return False

    def _on_text_edited(self, _buffer):
        # The text view is editable so OCR slips can be fixed before listening.
        if self._resynth_timer:
            GLib.source_remove(self._resynth_timer)
        self._resynth_timer = GLib.timeout_add(600, self._apply_edit)

    def _apply_edit(self):
        self._resynth_timer = None
        start, end = self.buffer.get_bounds()
        text = self.buffer.get_text(start, end, False)
        was_playing = self.player.playing
        self.player.pause()
        self._rebuild_sentences(text)
        if was_playing:
            self.player.play()
        return False

    def _on_voice_changed(self, dropdown, _param):
        if self._blocking_voice:
            return
        idx = dropdown.get_selected()
        if idx < 0 or idx >= len(VOICES):
            return
        vid = VOICES[idx][0]
        if vid == self.player.voice:
            return
        self.player.voice = vid
        self.cfg["voice"] = vid
        self.cfg.setdefault("voices_by_lang", {})[voice_iso(vid)] = vid
        self.player.lang_voices[voice_iso(vid)] = vid
        self._refresh_voice_hint(vid)
        save_config(self.cfg)
        self.player.restart_from(self.player.index)

    def _select_voice(self, vid):
        self._blocking_voice = True
        self.dropdown.set_selected(VOICE_INDEX.get(vid, 0))
        self._blocking_voice = False
        self._refresh_voice_hint(vid)

    def _refresh_voice_hint(self, vid):
        self.voice_hint.set_label(f"FOR {voice_iso(vid).upper()} TEXT")

    def _on_speed_changed(self, scale):
        value = round(scale.get_value(), 2)
        self.speed_label.set_label(f"{value:.2f}×")
        if abs(value - self.player.speed) < 1e-6:
            return
        self.player.speed = value
        self.cfg["speed"] = value
        save_config(self.cfg)
        if self._resynth_timer:
            GLib.source_remove(self._resynth_timer)
        self._resynth_timer = GLib.timeout_add(400, self._apply_speed)

    def _apply_speed(self):
        self._resynth_timer = None
        self.player.restart_from(self.player.index)
        return False

    def _on_volume_changed(self, scale):
        value = scale.get_value()
        self.vol_label.set_label(f"{int(round(value * 100))}%")
        self.player.volume = value          # applied live, no re-synthesis
        self.cfg["volume"] = round(value, 3)
        save_config(self.cfg)


# --------------------------------------------------------------------------- app


def read_all(stream):
    """Drain a GInputStream (the invoking process's stdin) into a string."""
    if stream is None:
        return ""
    chunks = []
    while True:
        data = stream.read_bytes(1 << 16, None).get_data()
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks).decode("utf-8", "replace")


class VoxboxApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.win = None
        self.restart_argv = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        THEME.apply()
        THEME.watch()

    def _window(self):
        if self.win is None:
            self.win = VoxboxWindow(self)
        return self.win

    def do_command_line(self, command_line):
        argv = command_line.get_arguments()[1:]
        if self._code_changed():
            # The file on disk moved on since this process started (an update,
            # an edit). The panel only hides on close, so without this the old
            # code would live on forever: quit and re-exec with the same request.
            self.restart_argv = argv
            self.quit()
            return 0
        action = argv[0] if argv else "show"
        fresh = self.win is None

        if action == "quit":
            self.quit()
            return 0
        if action in ("stop", "toggle") and fresh:
            # Nothing is playing because nothing is running. Don't boot a UI for it.
            return 0
        if action == "stop":
            self.win.player.stop()
            return 0
        if action == "toggle":
            self.win.player.toggle()
            return 0

        win = self._window()
        if action == "region":
            win.present()
            win.do_region()
        elif action == "selection":
            win.present()
            win.do_selection()
        elif action == "read":
            # get_stdin() is the caller's stdin even when this runs in an
            # already-open instance.
            data = read_all(command_line.get_stdin())
            win.present()
            if data.strip():
                win.load_text(data, source="stdin", autoplay=True)
        else:
            win.present()
        return 0

    def do_activate(self):
        self._window().present()

    @staticmethod
    def _code_changed():
        try:
            return CODE_PATH.stat().st_mtime != CODE_MTIME
        except OSError:
            return False


# --------------------------------------------------------------------------- daemon


class Daemon:
    """Headless mode for the Omarchy shell plugin: JSON lines on stdin/stdout.

    in:  {"cmd": "load", "text": ...} | play | pause | toggle | stop
         {"cmd": "jump", "delta": ±1} | {"cmd": "goto", "index": n}
         region | selection | {"cmd": "open", "path"} | {"cmd": "export", "path"}
         {"cmd": "set", "voice"|"speed"|"volume": value} | voices | status | quit
    out: {"event": "ready"|"text"|"sentence"|"state"|"error"|"export"|"config", ...}
    """

    def __init__(self):
        self.cfg = load_config()
        self.player = Player(self._on_sentence, self._on_state, self._on_error)
        self.player.volume = float(self.cfg["volume"])
        self.player.speed = float(self.cfg["speed"])
        self.player.voice = self.cfg["voice"] if self.cfg["voice"] in VOICE_INFO else DEFAULTS["voice"]
        self.player.lang_voices = {k: v for k, v in self.cfg.get("voices_by_lang", {}).items() if v in VOICE_INFO}
        self._sentences = []
        self._text = ""
        self._source = ""
        self._truncated = False
        self.loop = GLib.MainLoop()

    # -- output -------------------------------------------------------------

    def emit(self, **payload):
        line = json.dumps(payload, ensure_ascii=False)
        if len(line.encode("utf-8", "replace")) > MAX_EVENT_BYTES:
            # Never hand the shell an unbounded line; fail closed with a stub.
            line = json.dumps({"event": "error",
                               "message": f"{payload.get('event', 'event')} payload too large"})
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    def _on_sentence(self, idx):
        self.emit(event="sentence", index=idx, total=len(self._sentences),
                  lang=self.player.lang_of(idx))
        return False

    def _on_state(self, playing, finished):
        self.emit(event="state", playing=bool(playing), finished=bool(finished),
                  index=self.player.index, total=len(self._sentences))
        return False

    def _on_error(self, message):
        self.emit(event="error", message=str(message))
        return False

    # -- commands -----------------------------------------------------------

    def _send_text(self):
        # sentences only: the full text would double the payload for nothing.
        self.emit(event="text", sentences=self._sentences, source=self._source,
                  words=len(self._text.split()), truncated=self._truncated)

    def load(self, text, source="", autoplay=True):
        self._text = clean_text(text)
        self._truncated = len(self._text) > MAX_TEXT_CHARS
        if self._truncated:
            self._text = self._text[:MAX_TEXT_CHARS].rsplit(None, 1)[0]
        self._source = source
        self._sentences = split_sentences(self._text)
        self.player.load(self._sentences)
        self._send_text()
        if autoplay and self._sentences:
            self.player.play()

    def handle(self, msg):
        cmd = msg.get("cmd")
        if cmd == "load":
            self.load(msg.get("text", ""), msg.get("source", ""), msg.get("autoplay", True))
        elif cmd in ("play", "pause", "toggle", "stop"):
            getattr(self.player, cmd)()
        elif cmd == "jump":
            self.player.jump(int(msg.get("delta", 1)))
        elif cmd == "goto":
            self.player.goto(int(msg.get("index", 0)))
        elif cmd == "region":
            self.player.pause()
            threading.Thread(target=self._region, daemon=True).start()
        elif cmd == "selection":
            try:
                text = capture_selection()
            except InputTooLarge as exc:
                self.emit(event="error", message=str(exc))
                return
            if text:
                self.load(text, "selection")
            else:
                self.emit(event="error", message="nothing selected")
        elif cmd == "open":
            threading.Thread(target=self._open, args=(msg.get("path", ""),), daemon=True).start()
        elif cmd == "export":
            threading.Thread(target=self._export, args=(msg.get("path", ""),), daemon=True).start()
        elif cmd == "preview":
            threading.Thread(target=self._preview, args=(msg.get("voice", ""),), daemon=True).start()
        elif cmd == "set":
            self._set(msg)
        elif cmd == "voices":
            self.emit(event="voices", voices=[
                {"id": vid, "label": lbl, "iso": iso} for vid, lbl, iso, _e, _l in VOICES
            ])
        elif cmd == "status":
            self._send_text()
            self._on_state(self.player.playing, False)
            self.emit(event="config", voice=self.player.voice, speed=self.player.speed,
                      volume=self.player.volume)
        elif cmd == "quit":
            self.loop.quit()
        else:
            self.emit(event="error", message=f"unknown cmd: {cmd!r}")

    def _set(self, msg):
        if "voice" in msg and msg["voice"] in VOICE_INFO:
            vid = msg["voice"]
            self.player.voice = vid
            self.cfg["voice"] = vid
            self.cfg.setdefault("voices_by_lang", {})[voice_iso(vid)] = vid
            self.player.lang_voices[voice_iso(vid)] = vid
            self.player.restart_from(self.player.index)
        if "speed" in msg:
            self.player.speed = self.cfg["speed"] = round(float(msg["speed"]), 2)
            self.player.restart_from(self.player.index)
        if "volume" in msg:
            self.player.volume = float(msg["volume"])
            self.cfg["volume"] = round(self.player.volume, 3)
        save_config(self.cfg)
        self.emit(event="config", voice=self.player.voice, speed=self.player.speed,
                  volume=self.player.volume)

    def _region(self):
        time.sleep(0.25)   # let the triggering click's press/release finish
        try:
            text = capture_region(self.cfg.get("ocr_langs", "eng"), self._ocr_isos())
        except Exception as exc:
            GLib.idle_add(self.emit_error_idle, f"capture failed: {exc}")
            return
        GLib.idle_add(self._region_done, text)

    def emit_error_idle(self, message):
        self.emit(event="error", message=message)
        return False

    def _region_done(self, text):
        if text is None:
            self.emit(event="cancelled")
        elif not text.strip():
            self.emit(event="error", message="no text in that box")
        else:
            self.load(text, "region")
        return False

    def _ocr_isos(self):
        isos = [voice_iso(self.player.voice)]
        isos += [i for i in self.cfg.get("voices_by_lang", {}) if i not in isos]
        return isos[:6]

    def _open(self, path):
        try:
            text = open_document(path)
        except Exception as exc:
            GLib.idle_add(self.emit_error_idle, f"open failed: {exc}")
            return
        if text.strip():
            GLib.idle_add(lambda: (self.load(text, Path(path).name), False)[1])
        else:
            GLib.idle_add(self.emit_error_idle, "no readable text in file")

    def _export(self, path):
        if not self._text.strip():
            GLib.idle_add(self.emit_error_idle, "nothing to export")
            return
        if Path(path).suffix == "":
            path += ".mp3"
        try:
            samples = synthesize_all(
                self._text, self.player.voice, dict(self.player.lang_voices), self.player.speed,
                on_progress=lambda i, n: GLib.idle_add(
                    lambda: (self.emit(event="export", progress=f"{i}/{n}"), False)[1]),
            )
            encode_audio(samples, path, self.player.volume)
        except Exception as exc:
            GLib.idle_add(self.emit_error_idle, f"export failed: {exc}")
        else:
            GLib.idle_add(lambda: (self.emit(event="export", done=True, path=path), False)[1])

    def _preview(self, vid):
        if vid not in VOICE_INFO:
            return
        info = VOICE_INFO[vid]
        line = PREVIEW_LINES.get(info["iso"], PREVIEW_LINES["en"])
        try:
            samples = ENGINES[info["engine"]].synth(line, vid, self.player.speed, info["engine_lang"])
            GLib.idle_add(lambda: (self.player.preview(samples), False)[1])
        except Exception as exc:
            GLib.idle_add(self.emit_error_idle, str(exc))

    # -- stdin --------------------------------------------------------------

    def _on_stdin(self, channel, condition):
        if condition & GLib.IOCondition.HUP:
            self.loop.quit()
            return False
        line = channel.readline()
        if not line:
            self.loop.quit()
            return False
        line = line.strip()
        if line:
            try:
                self.handle(json.loads(line))
            except Exception as exc:
                self.emit(event="error", message=f"bad command: {exc}")
        return True

    def run(self):
        channel = GLib.IOChannel.unix_new(sys.stdin.fileno())
        GLib.io_add_watch(channel, GLib.PRIORITY_DEFAULT,
                          GLib.IOCondition.IN | GLib.IOCondition.HUP, self._on_stdin)
        self.emit(event="ready", voices=[{"id": v, "label": i["label"], "iso": i["iso"]}
                                         for v, i in VOICE_INFO.items()],
                  voice=self.player.voice, speed=self.player.speed, volume=self.player.volume,
                  languages=sorted(AVAILABLE_LANGS))
        self.loop.run()
        return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "daemon":
        return Daemon().run()
    if len(sys.argv) > 1 and sys.argv[1] == "theme":
        THEME.refresh()
        print(THEME.css() or "/* no Omarchy theme found */")
        return 0
    Adw.init()
    app = VoxboxApp()
    code = app.run(sys.argv)
    if app.restart_argv is not None:
        # Same PID, fresh code. The bus name is released as our socket closes on exec.
        sys.stdout.flush()
        os.execv(sys.executable, [sys.executable, str(CODE_PATH), *app.restart_argv])
    return code


if __name__ == "__main__":
    sys.exit(main())
