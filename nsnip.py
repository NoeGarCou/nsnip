#!/usr/bin/env python3
"""NSnip — a Windows 11 style Snipping Tool for Linux."""

import argparse
import importlib
import importlib.metadata
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, Gdk, GdkPixbuf, Gio, GLib
import cairo
from PIL import Image, ImageDraw

try:
    gi.require_version('Wnck', '3.0')
    from gi.repository import Wnck
except (ValueError, ImportError):
    Wnck = None

WAYLAND = 'wayland' in os.environ.get('XDG_SESSION_TYPE', '').lower()
APP_ID = 'io.github.noegarcou.NSnip'
APP_PATH = '/io/github/noegarcou/NSnip'
_GITHUB_REPO = 'NoeGarCou/nsnip'

SNIP_MODES = [
    ('rect',   'Rectangle'),
    ('free',   'Freeform'),
    ('window', 'Window'),
    ('screen', 'Full screen'),
]


# ── Annotation types ──────────────────────────────────────────────────────────

@dataclass
class Stroke:
    points: list
    color: tuple
    width: float
    highlight: bool = False

@dataclass
class Shape:
    kind: str  # 'rect' | 'ellipse' | 'line' | 'arrow'
    x1: float; y1: float; x2: float; y2: float
    color: tuple
    width: float

@dataclass
class TextMark:
    x: float; y: float
    text: str
    color: tuple
    size: float


# ── Screen capture ────────────────────────────────────────────────────────────

def capture_all_screens() -> tuple[Image.Image, cairo.ImageSurface, dict]:
    try:
        import mss
    except ImportError:
        sys.exit('mss not found. Run: pip install mss')
    with mss.MSS() as sct:
        bbox = sct.monitors[0]
        raw = sct.grab(bbox)
        w, h = raw.size
        data = bytearray(raw.bgra)
    img = Image.frombuffer('RGB', (w, h), bytes(data), 'raw', 'BGRX', 0, 1)
    # RGB24 ignores the X byte, so the raw buffer can back the surface directly
    surface = cairo.ImageSurface.create_for_data(data, cairo.FORMAT_RGB24, w, h, w * 4)
    return img, surface, bbox


# ── Clipboard & saving ────────────────────────────────────────────────────────

def copy_to_clipboard(image: Image.Image, store: bool = False):
    image = image.convert('RGB')
    w, h = image.size
    pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(image.tobytes()), GdkPixbuf.Colorspace.RGB, False, 8, w, h, w * 3
    )
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    clipboard.set_image(pixbuf)
    if store:
        clipboard.store()


def _default_save_dir() -> str:
    pictures = (GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
                or os.path.expanduser('~/Pictures'))
    return os.path.join(pictures, 'Screenshots')


def _save_dir(cfg: dict) -> str:
    return cfg.get('save_dir') or _default_save_dir()


def _timestamp_name() -> str:
    return datetime.now().strftime('Screenshot %Y-%m-%d %H%M%S.png')


def autosave(image: Image.Image, cfg: dict) -> str | None:
    folder = _save_dir(cfg)
    try:
        os.makedirs(folder, exist_ok=True)
        base, ext = os.path.splitext(_timestamp_name())
        path = os.path.join(folder, base + ext)
        n = 1
        while os.path.exists(path):
            n += 1
            path = os.path.join(folder, f'{base} ({n}){ext}')
        image.save(path)
        return path
    except OSError as e:
        print(f'NSnip: could not save screenshot: {e}', file=sys.stderr)
        return None


# ── Cairo helpers ─────────────────────────────────────────────────────────────

def _pil_to_surface(image: Image.Image) -> cairo.ImageSurface:
    data = bytearray(image.convert('RGBA').tobytes('raw', 'BGRA'))
    return cairo.ImageSurface.create_for_data(
        data, cairo.FORMAT_ARGB32, image.width, image.height, image.width * 4
    )


def _surface_to_pil(surface: cairo.ImageSurface) -> Image.Image:
    surface.flush()
    w, h = surface.get_width(), surface.get_height()
    # Cairo ARGB32 in memory (little-endian) = BGRA byte order
    return Image.frombuffer(
        'RGBA', (w, h), bytes(surface.get_data()), 'raw', 'BGRA', surface.get_stride(), 1
    ).convert('RGB')


def _set_text_font(cr: cairo.Context, size: float):
    cr.select_font_face('Sans', cairo.FontSlant.NORMAL, cairo.FontWeight.BOLD)
    cr.set_font_size(size)


def _text_extents(text: str, size: float) -> tuple[float, float]:
    cr = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))
    _set_text_font(cr, size)
    ascent, descent = cr.font_extents()[:2]
    return cr.text_extents(text).x_advance, ascent + descent


def _render_annotation(cr: cairo.Context, ann: Any, caret: bool = False):
    cr.save()
    cr.set_source_rgb(*ann.color)
    cr.set_line_cap(cairo.LineCap.ROUND)
    cr.set_line_join(cairo.LineJoin.ROUND)

    if isinstance(ann, Stroke):
        if ann.highlight:
            # Multiply keeps dark text readable under the marker, like a real highlighter
            cr.set_operator(cairo.Operator.MULTIPLY)
            cr.set_line_cap(cairo.LineCap.SQUARE)
        cr.set_line_width(ann.width)
        cr.move_to(*ann.points[0])
        for pt in ann.points[1:] or ann.points:
            cr.line_to(*pt)
        cr.stroke()

    elif isinstance(ann, Shape):
        cr.set_line_width(ann.width)
        x, y = min(ann.x1, ann.x2), min(ann.y1, ann.y2)
        w, h = abs(ann.x2 - ann.x1), abs(ann.y2 - ann.y1)
        if ann.kind == 'rect':
            cr.rectangle(x, y, w, h)
            cr.stroke()
        elif ann.kind == 'ellipse':
            if w > 0 and h > 0:
                cr.save()
                cr.translate(x + w / 2, y + h / 2)
                cr.scale(w / 2, h / 2)
                cr.arc(0, 0, 1, 0, 2 * math.pi)
                cr.restore()
                cr.stroke()
        else:
            angle = math.atan2(ann.y2 - ann.y1, ann.x2 - ann.x1)
            head = max(10.0, ann.width * 3.5)
            ex, ey = ann.x2, ann.y2
            if ann.kind == 'arrow':
                ex -= head * 0.6 * math.cos(angle)
                ey -= head * 0.6 * math.sin(angle)
            cr.move_to(ann.x1, ann.y1)
            cr.line_to(ex, ey)
            cr.stroke()
            if ann.kind == 'arrow':
                cr.move_to(ann.x2, ann.y2)
                for side in (-0.45, 0.45):
                    cr.line_to(ann.x2 - head * math.cos(angle + side),
                               ann.y2 - head * math.sin(angle + side))
                cr.close_path()
                cr.fill()

    elif isinstance(ann, TextMark):
        _set_text_font(cr, ann.size)
        ascent, descent = cr.font_extents()[:2]
        cr.move_to(ann.x, ann.y + ascent)
        cr.show_text(ann.text)
        if caret:
            cx = ann.x + cr.text_extents(ann.text).x_advance + 2
            cr.set_line_width(max(1.5, ann.size / 14))
            cr.move_to(cx, ann.y)
            cr.line_to(cx, ann.y + ascent + descent)
            cr.stroke()

    cr.restore()


def _seg_dist(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    if dx == dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _hit(ann: Any, x: float, y: float, r: float) -> bool:
    """True when (x, y) is within r of the annotation's ink."""
    if isinstance(ann, TextMark):
        w, h = _text_extents(ann.text, ann.size)
        return ann.x - r <= x <= ann.x + w + r and ann.y - r <= y <= ann.y + h + r

    if isinstance(ann, Stroke):
        pts = ann.points if len(ann.points) > 1 else ann.points * 2
    elif ann.kind == 'rect':
        x1, y1, x2, y2 = ann.x1, ann.y1, ann.x2, ann.y2
        pts = [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
    elif ann.kind == 'ellipse':
        cx, cy = (ann.x1 + ann.x2) / 2, (ann.y1 + ann.y2) / 2
        rx, ry = abs(ann.x2 - ann.x1) / 2, abs(ann.y2 - ann.y1) / 2
        pts = [(cx + rx * math.cos(a * math.pi / 24), cy + ry * math.sin(a * math.pi / 24))
               for a in range(49)]
    else:
        pts = [(ann.x1, ann.y1), (ann.x2, ann.y2)]

    tol = ann.width / 2 + r
    return any(_seg_dist(x, y, *pts[i], *pts[i + 1]) <= tol for i in range(len(pts) - 1))


# ── Icons ─────────────────────────────────────────────────────────────────────

def _draw_icon(cr: cairo.Context, name: str, rgba: Gdk.RGBA, accent: tuple | None = None):
    """Draw a 24×24 line icon in the widget's foreground colour."""
    cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, rgba.alpha)
    cr.set_line_width(1.6)
    cr.set_line_cap(cairo.LineCap.ROUND)
    cr.set_line_join(cairo.LineJoin.ROUND)

    if name == 'rect':
        cr.set_dash([3, 2.5])
        cr.rectangle(4, 6, 16, 12)
    elif name == 'free':
        cr.set_dash([3, 2.5])
        cr.move_to(6, 9)
        cr.curve_to(8, 3, 19, 4, 19, 10)
        cr.curve_to(19, 16, 13, 14, 11, 18)
        cr.curve_to(9, 21, 4, 15, 6, 9)
    elif name == 'window':
        cr.rectangle(3, 5, 18, 14)
        cr.move_to(3, 9); cr.line_to(21, 9)
    elif name == 'screen':
        cr.rectangle(3, 4, 18, 12)
        cr.move_to(12, 16); cr.line_to(12, 19)
        cr.move_to(8, 20);  cr.line_to(16, 20)
    elif name == 'pen':
        cr.move_to(5, 17); cr.line_to(15, 7); cr.line_to(17, 9)
        cr.line_to(7, 19); cr.line_to(4.5, 19.5); cr.close_path()
    elif name == 'highlighter':
        cr.move_to(7, 14); cr.line_to(14, 7); cr.line_to(18, 11); cr.line_to(11, 18)
        cr.close_path()
        cr.move_to(7, 14); cr.line_to(5, 18); cr.line_to(7, 19); cr.line_to(11, 18)
    elif name == 'eraser':
        cr.move_to(4, 15); cr.line_to(12, 7); cr.line_to(19, 14); cr.line_to(14, 19)
        cr.line_to(8, 19); cr.close_path()
        cr.move_to(8, 11); cr.line_to(15, 18)
        cr.move_to(14, 19); cr.line_to(20, 19)
    elif name == 'shape':
        cr.arc(9, 9, 5, 0, 2 * math.pi)
        cr.new_sub_path()
        cr.rectangle(11, 11, 9, 8)
    elif name == 'text':
        cr.move_to(6, 18); cr.line_to(12, 4); cr.line_to(18, 18)
        cr.move_to(8.4, 13); cr.line_to(15.6, 13)
    elif name == 'crop':
        cr.move_to(7, 3);  cr.line_to(7, 17);  cr.line_to(21, 17)
        cr.move_to(3, 7);  cr.line_to(17, 7);  cr.line_to(17, 21)
    cr.stroke()

    if accent is not None:
        cr.set_dash([])
        cr.set_source_rgb(*accent)
        cr.rectangle(4, 21, 16, 2.5)
        cr.fill()


class _Icon(Gtk.DrawingArea):
    def __init__(self, name: str, accent=None):
        super().__init__()
        self._name = name
        self._accent = accent  # callable returning an RGB tuple, or None
        self.set_size_request(24, 24)
        self.connect('draw', self._on_draw)

    def _on_draw(self, _w, cr):
        ctx = self.get_style_context()
        _draw_icon(cr, self._name, ctx.get_color(ctx.get_state()),
                   self._accent() if self._accent else None)


def _swatch(color: tuple, selected: bool) -> Gtk.Widget:
    da = Gtk.DrawingArea()
    da.set_size_request(22, 22)

    def draw(_w, cr):
        cr.arc(11, 11, 8, 0, 2 * math.pi)
        cr.set_source_rgb(*color)
        cr.fill_preserve()
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.6)
        cr.set_line_width(1)
        cr.stroke()
        if selected:
            cr.arc(11, 11, 10.5, 0, 2 * math.pi)
            cr.set_source_rgb(0.2, 0.55, 0.95)
            cr.set_line_width(2)
            cr.stroke()

    da.connect('draw', draw)
    return da


_CSS = b"""
.nsnip-snipbar {
    background-color: rgba(38, 38, 38, 0.97);
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 10px;
    padding: 5px;
}
.nsnip-snipbar button {
    background: none;
    border: none;
    box-shadow: none;
    text-shadow: none;
    color: #f2f2f2;
    min-width: 34px;
    min-height: 30px;
    border-radius: 6px;
    padding: 2px 4px;
}
.nsnip-snipbar button:hover   { background-color: rgba(255, 255, 255, 0.09); }
.nsnip-snipbar button:checked { background-color: rgba(255, 255, 255, 0.18); }
.nsnip-snipbar separator      { background-color: rgba(255, 255, 255, 0.15); margin: 4px 4px; }
button.nsnip-active {
    background-image: none;
    background-color: alpha(@theme_selected_bg_color, 0.28);
}
.nsnip-canvas { background-color: shade(@theme_bg_color, 0.88); }
"""


def _install_css():
    provider = Gtk.CssProvider()
    provider.load_from_data(_CSS)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )


# ── Config ────────────────────────────────────────────────────────────────────

_CONFIG_DIR  = os.path.expanduser('~/.config/nsnip')
_CONFIG_FILE = os.path.join(_CONFIG_DIR, 'config.json')
_DEFAULTS: dict = {
    'hotkey': '<Shift><Super>s',
    'print_screen': False,
    'print_taken': False,
    'auto_copy': True,
    'auto_save': True,
    'save_dir': '',
    'mode': 'rect',
}


def _load_config() -> dict:
    try:
        with open(_CONFIG_FILE) as f:
            cfg = {**_DEFAULTS, **json.load(f)}
    except (FileNotFoundError, json.JSONDecodeError):
        cfg = dict(_DEFAULTS)
    if '+' in cfg['hotkey']:  # pynput format from NSnip 0.1
        cfg['hotkey'] = _pynput_to_accel(cfg['hotkey'])
    return cfg


def _save_config(cfg: dict):
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    with open(_CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)


# ── Global shortcut ───────────────────────────────────────────────────────────
# Shortcuts are registered with the desktop (Cinnamon / GNOME custom keybindings),
# which works on X11 and Wayland. pynput is only a fallback for other desktops.

_MODIFIER_KEYSYMS = frozenset({
    Gdk.KEY_Control_L, Gdk.KEY_Control_R,
    Gdk.KEY_Shift_L,   Gdk.KEY_Shift_R,
    Gdk.KEY_Alt_L,     Gdk.KEY_Alt_R,
    Gdk.KEY_Super_L,   Gdk.KEY_Super_R,
    Gdk.KEY_Meta_L,    Gdk.KEY_Meta_R,
    Gdk.KEY_Hyper_L,   Gdk.KEY_Hyper_R,
    Gdk.KEY_ISO_Level3_Shift,
})

_GDK_TO_PYNPUT: dict[str, str] = {
    'Print': 'print_screen', 'Scroll_Lock': 'scroll_lock',
    'Num_Lock': 'num_lock',  'Pause': 'pause',
    'Insert': 'insert',      'Delete': 'delete',
    'Home': 'home',          'End': 'end',
    'Page_Up': 'page_up',    'Page_Down': 'page_down',
    'Up': 'up', 'Down': 'down', 'Left': 'left', 'Right': 'right',
    **{f'F{i}': f'f{i}' for i in range(1, 13)},
    'Escape': 'esc', 'Tab': 'tab', 'BackSpace': 'backspace',
    'Return': 'enter', 'space': 'space', 'Menu': 'menu',
}

_PYNPUT_MODS = {'ctrl': '<Control>', 'alt': '<Alt>', 'shift': '<Shift>',
                'super': '<Super>', 'cmd': '<Super>'}


def _pynput_to_accel(hotkey: str) -> str:
    gdk_names = {v: k for k, v in _GDK_TO_PYNPUT.items()}
    mods, key = '', ''
    for part in hotkey.split('+'):
        name = part.strip('<>')
        if name in _PYNPUT_MODS:
            mods += _PYNPUT_MODS[name]
        else:
            key = gdk_names.get(name, name)
    accel = mods + key
    return accel if Gtk.accelerator_parse(accel)[0] else _DEFAULTS['hotkey']


def _accel_to_pynput(accel: str) -> str | None:
    key, mods = Gtk.accelerator_parse(accel)
    if not key:
        return None
    parts = []
    if mods & Gdk.ModifierType.CONTROL_MASK: parts.append('<ctrl>')
    if mods & Gdk.ModifierType.MOD1_MASK:    parts.append('<alt>')
    if mods & Gdk.ModifierType.SUPER_MASK:   parts.append('<cmd>')
    if mods & Gdk.ModifierType.SHIFT_MASK:   parts.append('<shift>')
    name = Gdk.keyval_name(key)
    if name in _GDK_TO_PYNPUT:
        parts.append(f'<{_GDK_TO_PYNPUT[name]}>')
    elif len(name) == 1:
        parts.append(name.lower())
    else:
        return None
    return '+'.join(parts)


def _accel_label(accel: str) -> str:
    key, mods = Gtk.accelerator_parse(accel)
    return Gtk.accelerator_get_label(key, mods) if key else accel


def _schema_exists(schema_id: str) -> bool:
    source = Gio.SettingsSchemaSource.get_default()
    return source is not None and source.lookup(schema_id, True) is not None


_CINNAMON_KB = 'org.cinnamon.desktop.keybindings'
_GNOME_KB = 'org.gnome.settings-daemon.plugins.media-keys'


def _shortcut_backend() -> str | None:
    desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()
    if 'cinnamon' in desktop and _schema_exists(_CINNAMON_KB):
        return 'cinnamon'
    if 'gnome' in desktop and _schema_exists(_GNOME_KB):
        return 'gnome'
    return None


def _snip_command() -> str:
    """Ask the running NSnip over D-Bus (instant); start a one-shot instance otherwise."""
    nsnip_bin = shutil.which('nsnip') or os.path.abspath(sys.argv[0])
    remote = (f'gdbus call --session --dest {APP_ID} --object-path {APP_PATH} '
              f'--method org.gtk.Actions.Activate snip [] {{}}')
    return f"sh -c '{remote} >/dev/null 2>&1 || {nsnip_bin} --snip'"


def _set_if_changed(settings: Gio.Settings, key: str, value) -> bool:
    if isinstance(value, list):
        if list(settings.get_strv(key)) == value:
            return False
        settings.set_strv(key, value)
    else:
        if settings.get_string(key) == value:
            return False
        settings.set_string(key, value)
    return True


def _apply_cinnamon(cfg: dict):
    base = Gio.Settings.new(_CINNAMON_KB)
    kb = Gio.Settings.new_with_path(
        f'{_CINNAMON_KB}.custom-keybinding',
        '/org/cinnamon/desktop/keybindings/custom-keybindings/nsnip/',
    )
    bindings = [cfg['hotkey']] + (['Print'] if cfg['print_screen'] else [])
    changed = _set_if_changed(kb, 'name', 'NSnip')
    changed |= _set_if_changed(kb, 'command', _snip_command())
    changed |= _set_if_changed(kb, 'binding', bindings)

    names = list(base.get_strv('custom-list'))
    if 'nsnip' not in names:
        names.insert(0, 'nsnip')
        changed = True
    if changed:
        # Cinnamon only reloads custom bindings when the list itself changes
        if '__dummy__' in names:
            names.remove('__dummy__')
        else:
            names.append('__dummy__')
        base.set_strv('custom-list', names)

    # Free the Print key from Cinnamon's own screenshot tool (and give it back later)
    media = Gio.Settings.new(f'{_CINNAMON_KB}.media-keys')
    shot = list(media.get_strv('screenshot'))
    if cfg['print_screen'] and 'Print' in shot:
        shot.remove('Print')
        media.set_strv('screenshot', shot)
        cfg['print_taken'] = True
        _save_config(cfg)
    elif not cfg['print_screen'] and cfg.get('print_taken'):
        if 'Print' not in shot:
            media.set_strv('screenshot', shot + ['Print'])
        cfg['print_taken'] = False
        _save_config(cfg)


def _apply_gnome(cfg: dict):
    path = '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/nsnip/'
    base = Gio.Settings.new(_GNOME_KB)
    kb = Gio.Settings.new_with_path(f'{_GNOME_KB}.custom-keybinding', path)
    _set_if_changed(kb, 'name', 'NSnip')
    _set_if_changed(kb, 'command', _snip_command())
    _set_if_changed(kb, 'binding', cfg['hotkey'])
    paths = list(base.get_strv('custom-keybindings'))
    if path not in paths:
        base.set_strv('custom-keybindings', paths + [path])


def apply_shortcuts(cfg: dict) -> bool:
    """Register the snip shortcut with the desktop. False if unsupported."""
    backend = _shortcut_backend()
    if backend == 'cinnamon':
        _apply_cinnamon(cfg)
    elif backend == 'gnome':
        _apply_gnome(cfg)
    else:
        return False
    Gio.Settings.sync()
    return True


# ── Launcher & autostart ──────────────────────────────────────────────────────

_AUTOSTART_FILE = os.path.expanduser('~/.config/autostart/nsnip.desktop')


def _desktop_entry(autostart: bool | None = None) -> str:
    nsnip_bin = shutil.which('nsnip') or 'nsnip'
    entry = (
        '[Desktop Entry]\n'
        'Name=NSnip\n'
        'Comment=Snipping tool: capture, mark up and share screenshots\n'
        f'Exec={nsnip_bin}\n'
        'Icon=nsnip\n'
        'Type=Application\n'
        'Categories=Graphics;Utility;\n'
        'StartupNotify=false\n'
        'Actions=snip;\n'
    )
    if autostart is not None:
        entry += f'X-GNOME-Autostart-enabled={"true" if autostart else "false"}\n'
    entry += (
        '\n[Desktop Action snip]\n'
        'Name=New snip\n'
        f'Exec={nsnip_bin} --snip\n'
    )
    return entry


def _autostart_enabled() -> bool:
    kf = GLib.KeyFile()
    try:
        kf.load_from_file(_AUTOSTART_FILE, GLib.KeyFileFlags.NONE)
    except GLib.Error:
        return False
    for key in ('Hidden', 'X-GNOME-Autostart-enabled'):
        try:
            value = kf.get_boolean('Desktop Entry', key)
        except GLib.Error:
            continue
        if value == (key == 'Hidden'):
            return False
    return True


def _set_autostart(enabled: bool):
    os.makedirs(os.path.dirname(_AUTOSTART_FILE), exist_ok=True)
    with open(_AUTOSTART_FILE, 'w') as f:
        f.write(_desktop_entry(autostart=enabled))


# ── Update checking ───────────────────────────────────────────────────────────

def _remote_version() -> str | None:
    url = f'https://raw.githubusercontent.com/{_GITHUB_REPO}/main/pyproject.toml'
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            for line in r.read().decode().splitlines():
                if line.startswith('version'):
                    return line.split('"')[1]
    except Exception:
        return None


def _local_version() -> str:
    try:
        return importlib.metadata.version('nsnip')
    except importlib.metadata.PackageNotFoundError:
        return '0.0.0'


def _ver_tuple(v: str) -> tuple:
    try:
        return tuple(map(int, v.split('.')))
    except ValueError:
        return (0,)


def _show_info(title: str, msg: str):
    dlg = Gtk.MessageDialog(
        transient_for=None, modal=False,
        message_type=Gtk.MessageType.INFO,
        buttons=Gtk.ButtonsType.OK,
        text=title, secondary_text=msg,
    )
    dlg.run(); dlg.destroy()


def _do_update():
    subprocess.Popen(
        ['notify-send', '-i', 'system-software-update', '-t', '5000',
         'NSnip', 'Updating… will restart when done.'],
        stderr=subprocess.DEVNULL,
    )

    def _run():
        proc = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '--user',
             '--break-system-packages', '--upgrade',
             f'git+https://github.com/{_GITHUB_REPO}.git'],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            GLib.idle_add(_restart)
        else:
            detail = (proc.stderr or proc.stdout)[-500:]
            GLib.idle_add(lambda: _show_info('Update failed', detail))

    threading.Thread(target=_run, daemon=True).start()


def _restart():
    nsnip_bin = shutil.which('nsnip') or sys.argv[0]
    try:
        os.execv(nsnip_bin, [nsnip_bin])
    except OSError as e:
        _show_info('Restart failed', str(e))
    return GLib.SOURCE_REMOVE


def check_for_updates():
    def _run():
        local  = _local_version()
        remote = _remote_version()

        def _show():
            if remote is None:
                _show_info('Update check failed',
                           'Could not reach GitHub. Check your connection.')
            elif _ver_tuple(remote) > _ver_tuple(local):
                dlg = Gtk.MessageDialog(
                    transient_for=None, modal=False,
                    message_type=Gtk.MessageType.QUESTION,
                    buttons=Gtk.ButtonsType.YES_NO,
                    text=f'Update available: {remote}',
                    secondary_text=f'Installed: {local}\nUpdate now and restart?',
                )
                resp = dlg.run(); dlg.destroy()
                if resp == Gtk.ResponseType.YES:
                    _do_update()
            else:
                _show_info('NSnip is up to date',
                           f'You are running the latest version ({local}).')

        GLib.idle_add(_show)

    threading.Thread(target=_run, daemon=True).start()


# ── Settings dialog ───────────────────────────────────────────────────────────

class _HotkeyCaptureDialog(Gtk.Dialog):
    def __init__(self, parent):
        super().__init__(title='Set Shortcut', transient_for=parent, modal=True)
        self.set_default_size(300, 90)
        self.captured: str | None = None

        lbl = Gtk.Label(label='Press a key combination…')
        lbl.set_margin_start(16); lbl.set_margin_end(16)
        lbl.set_margin_top(20);   lbl.set_margin_bottom(20)
        self.get_content_area().add(lbl)
        self.add_button('Cancel', Gtk.ResponseType.CANCEL)
        self.show_all()
        self.connect('key-press-event', self._on_key)
        self.run()
        self.destroy()

    def _on_key(self, _win, event):
        if event.keyval in _MODIFIER_KEYSYMS:
            return
        keymap = Gdk.Keymap.get_for_display(self.get_display())
        state = keymap.add_virtual_modifiers(event.state)
        mods = state & Gtk.accelerator_get_default_mod_mask()
        if mods & Gdk.ModifierType.SUPER_MASK:
            mods &= ~Gdk.ModifierType.MOD4_MASK
        self.captured = Gtk.accelerator_name(Gdk.keyval_to_lower(event.keyval), mods)
        self.response(Gtk.ResponseType.OK)


class PreferencesDialog(Gtk.Dialog):
    def __init__(self, app: 'NSnipApp', parent: Gtk.Window | None = None):
        super().__init__(title='NSnip Settings', transient_for=parent, modal=False)
        self.set_default_size(460, -1)
        cfg = app.cfg
        self._hotkey = cfg['hotkey']
        backend = _shortcut_backend()

        grid = Gtk.Grid(column_spacing=24, row_spacing=12)
        grid.set_margin_start(18); grid.set_margin_end(18)
        grid.set_margin_top(18);   grid.set_margin_bottom(18)
        row = 0

        def add_row(label: str, widget: Gtk.Widget, hint: str | None = None):
            nonlocal row
            lbl = Gtk.Label(label=label, halign=Gtk.Align.START, hexpand=True, xalign=0)
            if hint:
                lbl.set_tooltip_text(hint)
            widget.set_halign(Gtk.Align.END)
            widget.set_valign(Gtk.Align.CENTER)
            grid.attach(lbl, 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)
            row += 1

        def switch(active: bool) -> Gtk.Switch:
            sw = Gtk.Switch()
            sw.set_active(active)
            return sw

        self._key_btn = Gtk.Button(label=_accel_label(self._hotkey))
        self._key_btn.set_tooltip_text('Click to change the shortcut')
        self._key_btn.connect('clicked', self._on_change_key)
        add_row('Snip shortcut', self._key_btn)

        self._print_sw = switch(cfg['print_screen'])
        self._print_sw.set_sensitive(backend == 'cinnamon')
        add_row('Use the Print Screen key to open snipping', self._print_sw,
                'Replaces the desktop\'s own Print Screen action (Cinnamon only)')

        self._copy_sw = switch(cfg['auto_copy'])
        add_row('Automatically copy changes', self._copy_sw,
                'Edits made in the editor are copied to the clipboard as you go')

        self._save_sw = switch(cfg['auto_save'])
        add_row('Automatically save screenshots', self._save_sw)

        folder = _save_dir(cfg)
        os.makedirs(folder, exist_ok=True)
        self._folder_btn = Gtk.FileChooserButton(
            title='Screenshots folder', action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        self._folder_btn.set_filename(folder)
        add_row('Save screenshots to', self._folder_btn)

        self._login_sw = switch(_autostart_enabled())
        add_row('Start NSnip when you log in', self._login_sw,
                'Keeps NSnip in the tray so snips open instantly')

        if backend is None:
            note = Gtk.Label(
                label='Your desktop is not Cinnamon or GNOME: the shortcut only works '
                      'while NSnip runs in the tray.',
                wrap=True, xalign=0,
            )
            note.get_style_context().add_class('dim-label')
            grid.attach(note, 0, row, 2, 1)
            row += 1

        update_btn = Gtk.Button(label='Check for updates')
        update_btn.connect('clicked', lambda _: check_for_updates())
        add_row(f'Version {_local_version()}', update_btn)

        self.get_content_area().add(grid)
        self.add_button('Cancel', Gtk.ResponseType.CANCEL)
        save_btn = self.add_button('Save', Gtk.ResponseType.OK)
        save_btn.get_style_context().add_class('suggested-action')

        self.show_all()
        if self.run() == Gtk.ResponseType.OK:
            cfg['hotkey'] = self._hotkey
            cfg['print_screen'] = self._print_sw.get_active()
            cfg['auto_copy'] = self._copy_sw.get_active()
            cfg['auto_save'] = self._save_sw.get_active()
            chosen = self._folder_btn.get_filename() or ''
            cfg['save_dir'] = '' if chosen == _default_save_dir() else chosen
            _save_config(cfg)
            if self._login_sw.get_active() != _autostart_enabled():
                _set_autostart(self._login_sw.get_active())
            app.apply_settings()
        self.destroy()

    def _on_change_key(self, _btn):
        cap = _HotkeyCaptureDialog(self)
        if cap.captured:
            self._hotkey = cap.captured
            self._key_btn.set_label(_accel_label(cap.captured))


# ── Snip overlay ──────────────────────────────────────────────────────────────

def _list_windows(origin: tuple[int, int]) -> list[tuple]:
    """Visible top-level window rects in image coords, topmost first."""
    if Wnck is None or WAYLAND:
        return []
    screen = Wnck.Screen.get_default()
    if screen is None:
        return []
    screen.force_update()
    workspace = screen.get_active_workspace()
    wanted = {Wnck.WindowType.NORMAL, Wnck.WindowType.DIALOG, Wnck.WindowType.UTILITY}
    rects = []
    for win in reversed(screen.get_windows_stacked()):
        if (win.get_window_type() not in wanted or win.is_minimized()
                or (workspace and not win.is_visible_on_workspace(workspace))):
            continue
        x, y, w, h = win.get_geometry()
        rects.append((x - origin[0], y - origin[1], w, h))
    return rects


def _norm_rect(x1, y1, x2, y2) -> tuple:
    return min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1)


def _contains(rect, x, y) -> bool:
    rx, ry, rw, rh = rect
    return rx <= x < rx + rw and ry <= y < ry + rh


def _freeform_crop(image: Image.Image, path: list) -> Image.Image:
    xs, ys = [p[0] for p in path], [p[1] for p in path]
    x1, y1 = max(0, int(min(xs))), max(0, int(min(ys)))
    x2, y2 = min(image.width, int(max(xs)) + 1), min(image.height, int(max(ys)) + 1)
    crop = image.crop((x1, y1, x2, y2))
    mask = Image.new('L', crop.size, 0)
    ImageDraw.Draw(mask).polygon([(x - x1, y - y1) for x, y in path], fill=255)
    out = Image.new('RGB', crop.size, (255, 255, 255))
    out.paste(crop, (0, 0), mask)
    return out


class SnipSession:
    """Freezes every screen and lets the user pick a region, window or monitor.

    All coordinates are physical pixels relative to the top-left of the capture.
    """

    def __init__(self, app: 'NSnipApp', mode: str, on_done):
        self._app = app
        self._on_done = on_done
        self.mode = mode
        self.image, self.surface, bbox = capture_all_screens()
        self.origin = (bbox['left'], bbox['top'])
        self.windows = _list_windows(self.origin)

        display = Gdk.Display.get_default()
        self.scale = display.get_monitor(0).get_scale_factor()
        self.monitors: list[tuple] = []
        self.drag_start: tuple | None = None
        self.drag_end: tuple | None = None
        self.path: list = []
        self.hover: tuple | None = None
        self._finished = False

        pointer = display.get_default_seat().get_pointer()
        _screen, px, py = pointer.get_position()
        pointer_mon = display.get_monitor_at_point(px, py)

        self._overlays: list[_SnipOverlay] = []
        s = self.scale
        for i in range(display.get_n_monitors()):
            mon = display.get_monitor(i)
            g = mon.get_geometry()
            rect = (g.x * s - self.origin[0], g.y * s - self.origin[1], g.width * s, g.height * s)
            self.monitors.append(rect)
            win = _SnipOverlay(self, i, g, rect, with_toolbar=(mon == pointer_mon))
            app.add_window(win)
            self._overlays.append(win)

        self.motion(px * s - self.origin[0], py * s - self.origin[1])
        for win in self._overlays:
            win.show_all()
        for win in self._overlays:
            if win.has_toolbar:
                win.present()
                win.grab_keyboard()

    # ── Selection state ──────────────────────────────────────────────────────

    def set_mode(self, mode: str):
        self.mode = mode
        self.drag_start = self.drag_end = None
        self.path = []
        self.hover = None
        self._app.cfg['mode'] = mode
        _save_config(self._app.cfg)
        self.redraw()

    def region(self) -> tuple | None:
        """('rect', rect) | ('path', points) describing what is highlighted."""
        if self.mode == 'rect' and self.drag_start:
            return 'rect', _norm_rect(*self.drag_start, *self.drag_end)
        if self.mode == 'free' and len(self.path) > 1:
            return 'path', self.path
        if self.mode in ('window', 'screen') and self.hover:
            return 'rect', self.hover
        return None

    def press(self, x, y):
        if self.mode in ('rect', 'free'):
            self.drag_start = self.drag_end = (x, y)
            self.path = [(x, y)]

    def motion(self, x, y):
        if self.drag_start:
            self.drag_end = (x, y)
            if self.mode == 'free':
                self.path.append((x, y))
        elif self.mode in ('window', 'screen'):
            under = [r for r in (self.windows if self.mode == 'window' else []) if _contains(r, x, y)]
            under += [r for r in self.monitors if _contains(r, x, y)]
            self.hover = self._clip(under[0]) if under else None
        self.redraw()

    def release(self, x, y):
        if self.mode == 'rect' and self.drag_start:
            rx, ry, rw, rh = self._clip(_norm_rect(*self.drag_start, x, y))
            self.drag_start = None
            if rw >= 5 and rh >= 5:
                self.finish(self.image.crop((rx, ry, rx + rw, ry + rh)))
        elif self.mode == 'free' and self.drag_start:
            self.drag_start = None
            xs, ys = [p[0] for p in self.path], [p[1] for p in self.path]
            if len(self.path) > 2 and max(xs) - min(xs) >= 5 and max(ys) - min(ys) >= 5:
                self.finish(_freeform_crop(self.image, self.path))
            self.path = []
        elif self.hover:
            rx, ry, rw, rh = self.hover
            self.finish(self.image.crop((rx, ry, rx + rw, ry + rh)))
        self.redraw()

    def _clip(self, rect) -> tuple:
        x, y, w, h = rect
        x1, y1 = max(0, int(x)), max(0, int(y))
        x2, y2 = min(self.image.width, int(x + w)), min(self.image.height, int(y + h))
        return x1, y1, max(0, x2 - x1), max(0, y2 - y1)

    def redraw(self):
        for win in self._overlays:
            win.queue_draw()

    def cancel(self):
        self.finish(None)

    def finish(self, image: Image.Image | None):
        if self._finished:
            return
        self._finished = True
        Gdk.Display.get_default().get_default_seat().ungrab()
        for win in self._overlays:
            win.destroy()
        # Let the overlays unmap before the clipboard / notification work happens
        GLib.idle_add(self._on_done, image)


class _SnipOverlay(Gtk.Window):
    """Fullscreen frozen copy of one monitor."""

    def __init__(self, session: SnipSession, index: int, geom: Gdk.Rectangle,
                 rect: tuple, with_toolbar: bool):
        super().__init__(title='NSnip')
        self._session = session
        self._rect = rect
        self.has_toolbar = with_toolbar

        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_app_paintable(True)
        self.move(geom.x, geom.y)
        self.set_default_size(geom.width, geom.height)
        self.fullscreen_on_monitor(self.get_screen(), index)

        overlay = Gtk.Overlay()
        da = Gtk.DrawingArea()
        da.set_events(
            Gdk.EventMask.BUTTON_PRESS_MASK |
            Gdk.EventMask.BUTTON_RELEASE_MASK |
            Gdk.EventMask.POINTER_MOTION_MASK
        )
        da.connect('draw', self._on_draw)
        da.connect('button-press-event', self._on_press)
        da.connect('motion-notify-event', self._on_motion)
        da.connect('button-release-event', self._on_release)
        overlay.add(da)
        if with_toolbar:
            overlay.add_overlay(self._build_toolbar())
        self.add(overlay)

        self.connect('key-press-event', self._on_key)
        self.connect('realize', lambda w: w.get_window().set_cursor(
            Gdk.Cursor.new_from_name(w.get_display(), 'crosshair')
        ))

    def _build_toolbar(self) -> Gtk.Widget:
        bar = Gtk.Box(spacing=2, halign=Gtk.Align.CENTER, valign=Gtk.Align.START)
        bar.set_margin_top(18)
        bar.get_style_context().add_class('nsnip-snipbar')

        group = None
        for key, label in SNIP_MODES:
            btn = Gtk.RadioButton.new_from_widget(group)
            group = group or btn
            btn.set_mode(False)
            btn.add(_Icon(key))
            btn.set_tooltip_text(label)
            btn.set_active(key == self._session.mode)
            btn.connect('toggled', lambda b, k=key: b.get_active() and self._session.set_mode(k))
            btn.set_can_focus(False)
            bar.pack_start(btn, False, False, 0)

        bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)

        close = Gtk.Button()
        close.add(Gtk.Image.new_from_icon_name('window-close-symbolic', Gtk.IconSize.BUTTON))
        close.set_tooltip_text('Close  (Esc)')
        close.set_can_focus(False)
        close.connect('clicked', lambda _: self._session.cancel())
        bar.pack_start(close, False, False, 0)
        return bar

    def grab_keyboard(self, attempt: int = 0):
        """Grab the keyboard so Esc works no matter which window had focus."""
        gdk_win = self.get_window()
        if gdk_win is not None and gdk_win.is_viewable():
            status = self.get_display().get_default_seat().grab(
                gdk_win, Gdk.SeatCapabilities.KEYBOARD, True, None, None, None, None
            )
            if status == Gdk.GrabStatus.SUCCESS:
                return False
        if attempt < 20:
            GLib.timeout_add(25, self.grab_keyboard, attempt + 1)
        return False

    def _img_xy(self, event) -> tuple[float, float]:
        s, (ox, oy) = self._session.scale, self._session.origin
        return event.x_root * s - ox, event.y_root * s - oy

    def _on_press(self, _w, event):
        if event.button == 1:
            self._session.press(*self._img_xy(event))

    def _on_motion(self, _w, event):
        self._session.motion(*self._img_xy(event))

    def _on_release(self, _w, event):
        if event.button == 1:
            self._session.release(*self._img_xy(event))

    def _on_key(self, _w, event):
        if event.keyval == Gdk.KEY_Escape:
            self._session.cancel()
            return True
        return False

    def _on_draw(self, _w, cr: cairo.Context):
        s = self._session
        mx, my, mw, mh = self._rect
        cr.scale(1 / s.scale, 1 / s.scale)
        cr.translate(-mx, -my)
        cr.set_source_surface(s.surface, 0, 0)
        cr.paint()

        region = s.region()

        def region_path():
            kind, data = region
            if kind == 'rect':
                cr.rectangle(*data)
            else:
                cr.move_to(*data[0])
                for pt in data[1:]:
                    cr.line_to(*pt)
                cr.close_path()

        cr.rectangle(mx, my, mw, mh)
        if region:
            region_path()
        cr.set_fill_rule(cairo.FillRule.EVEN_ODD)
        cr.set_source_rgba(0, 0, 0, 0.45)
        cr.fill()
        if not region:
            return

        region_path()
        cr.set_source_rgb(1, 1, 1)
        cr.set_line_width(2 * s.scale)
        cr.stroke()

        if region[0] == 'rect' and s.mode == 'rect':
            x, y, w, h = region[1]
            label = f'{int(w)} × {int(h)}'
            _set_text_font(cr, 12 * s.scale)
            te = cr.text_extents(label)
            pad = 5 * s.scale
            lx = x
            ly = y - te.height - 3 * pad if y - te.height - 3 * pad > my else y + h + pad
            cr.set_source_rgba(0.12, 0.12, 0.12, 0.9)
            cr.rectangle(lx, ly, te.x_advance + 2 * pad, te.height + 2 * pad)
            cr.fill()
            cr.set_source_rgb(1, 1, 1)
            cr.move_to(lx + pad, ly + pad - te.y_bearing)
            cr.show_text(label)


# ── Editor window ─────────────────────────────────────────────────────────────

_PALETTE = [
    (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), (0.55, 0.55, 0.55), (0.91, 0.15, 0.15),
    (1.0, 0.55, 0.0), (1.0, 0.85, 0.0), (0.18, 0.7, 0.25), (0.1, 0.75, 0.85),
    (0.1, 0.4, 0.95), (0.55, 0.25, 0.85), (0.95, 0.4, 0.7), (0.5, 0.3, 0.15),
]
_HIGHLIGHTS = [
    (1.0, 0.92, 0.2), (0.55, 0.95, 0.35), (0.4, 0.9, 1.0),
    (1.0, 0.6, 0.85), (1.0, 0.7, 0.3), (0.75, 0.6, 1.0),
]
_SHAPES = [('rect', 'Rectangle'), ('ellipse', 'Oval'), ('line', 'Line'), ('arrow', 'Arrow')]

_TOOLS = [
    ('pen',         'Ballpoint pen'),
    ('highlighter', 'Highlighter'),
    ('eraser',      'Eraser  (click or drag over marks)'),
    ('shape',       'Shapes'),
    ('text',        'Text'),
]
_HANDLES = ('nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w')


class EditorWindow(Gtk.ApplicationWindow):
    def __init__(self, app: 'NSnipApp', image: Image.Image, path: str | None = None):
        super().__init__(application=app, title='NSnip')
        self._app = app
        self._tool = 'pen'
        self._prev_tool = 'pen'
        self._settings = {
            'pen':         {'color': (0.91, 0.15, 0.15), 'size': 4.0},
            'highlighter': {'color': (1.0, 0.92, 0.2), 'size': 18.0},
            'shape':       {'color': (0.91, 0.15, 0.15), 'size': 4.0, 'kind': 'rect'},
            'text':        {'color': (0.91, 0.15, 0.15), 'size': 28.0},
        }
        self._zoom = 1.0
        self._fit = True
        self._copy_pending = False
        self._im = Gtk.IMMulticontext()
        self._im.connect('commit', self._on_im_commit)

        self._build_ui()
        self.load_image(image, path)
        self._size_to_image()

        self.connect('key-press-event', self._on_key)

    # ── Image state ──────────────────────────────────────────────────────────

    def load_image(self, image: Image.Image, path: str | None = None):
        self._image = image.convert('RGB')
        self._path = path
        self._bg = _pil_to_surface(self._image)
        self._anns: list = []
        self._crop = (0, 0, self._image.width, self._image.height)
        self._undo: list = []
        self._redo: list = []
        self._current: Any = None
        self._text_edit: TextMark | None = None
        self._erase_snapshot: list | None = None
        self._crop_edit: list | None = None
        self._crop_drag: tuple | None = None
        if self._tool == 'crop':
            self._tool = self._prev_tool
        self._crop_bar.set_reveal_child(False)
        self._update_tool_buttons()
        self._fit = True
        self._apply_zoom()
        self._update_history_buttons()

    def _commit(self, anns: list, crop: tuple | None = None):
        self._undo.append((list(self._anns), self._crop))
        self._redo.clear()
        self._anns = anns
        if crop is not None:
            self._crop = crop
        self._changed()

    def _changed(self):
        self._update_history_buttons()
        self._apply_zoom()
        self._da.queue_draw()
        if self._app.cfg['auto_copy'] and not self._copy_pending:
            self._copy_pending = True
            GLib.timeout_add(350, self._auto_copy)

    def _auto_copy(self):
        self._copy_pending = False
        copy_to_clipboard(self.flatten())
        return GLib.SOURCE_REMOVE

    def _undo_action(self):
        self._finish_text()
        if self._undo:
            self._redo.append((list(self._anns), self._crop))
            self._anns, self._crop = self._undo.pop()
            self._changed()

    def _redo_action(self):
        if self._redo:
            self._undo.append((list(self._anns), self._crop))
            self._anns, self._crop = self._redo.pop()
            self._changed()

    def flatten(self) -> Image.Image:
        self._finish_text()
        x, y, w, h = self._crop
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(surface)
        cr.translate(-x, -y)
        cr.set_source_surface(self._bg, 0, 0)
        cr.paint()
        for ann in self._anns:
            _render_annotation(cr, ann)
        return _surface_to_pil(surface)

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        header = Gtk.HeaderBar(show_close_button=True)
        self.set_titlebar(header)

        new_box = Gtk.Box()
        new_box.get_style_context().add_class('linked')
        new_btn = Gtk.Button()
        inner = Gtk.Box(spacing=6)
        inner.pack_start(Gtk.Image.new_from_icon_name('list-add-symbolic', Gtk.IconSize.BUTTON),
                         False, False, 0)
        inner.pack_start(Gtk.Label(label='New'), False, False, 0)
        new_btn.add(inner)
        new_btn.set_tooltip_text('New snip  (Ctrl+N)')
        new_btn.connect('clicked', lambda _: self._new_snip())
        new_box.pack_start(new_btn, False, False, 0)
        mode_btn = Gtk.MenuButton(popover=self._build_new_popover())
        mode_btn.set_tooltip_text('Snipping mode and delay')
        new_box.pack_start(mode_btn, False, False, 0)
        header.pack_start(new_box)

        tools = Gtk.Box(spacing=2)
        self._undo_btn = self._icon_button('edit-undo-symbolic', 'Undo  (Ctrl+Z)', self._undo_action)
        self._redo_btn = self._icon_button('edit-redo-symbolic', 'Redo  (Ctrl+Y)', self._redo_action)
        tools.pack_start(self._undo_btn, False, False, 0)
        tools.pack_start(self._redo_btn, False, False, 0)
        tools.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 6)

        self._tool_btns: dict[str, Gtk.Button] = {}
        for key, label in _TOOLS + [('crop', 'Crop')]:
            if key == 'crop':
                tools.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 6)
            btn = Gtk.Button()
            btn.set_relief(Gtk.ReliefStyle.NONE)
            accent = (lambda k=key: self._settings[k]['color']) if key in self._settings else None
            btn.add(_Icon(key, accent))
            hint = ('click again for colours and size' if key in self._settings else '')
            btn.set_tooltip_text(f'{label}\n{hint}'.strip())
            btn.connect('clicked', lambda b, k=key: self._on_tool_clicked(k, b))
            self._tool_btns[key] = btn
            tools.pack_start(btn, False, False, 0)
        header.set_custom_title(tools)

        menu_btn = Gtk.MenuButton(popover=self._build_menu_popover())
        menu_btn.add(Gtk.Image.new_from_icon_name('open-menu-symbolic', Gtk.IconSize.BUTTON))
        header.pack_end(menu_btn)
        header.pack_end(self._icon_button('edit-copy-symbolic', 'Copy  (Ctrl+C)', self._copy))
        header.pack_end(self._icon_button('document-save-symbolic', 'Save as  (Ctrl+S)', self._save_as))
        self._zoom_btn = Gtk.Button(label='100%')
        self._zoom_btn.set_relief(Gtk.ReliefStyle.NONE)
        self._zoom_btn.set_tooltip_text('Fit / actual size  (Ctrl+scroll to zoom)')
        self._zoom_btn.connect('clicked', lambda _: self._toggle_fit())
        header.pack_end(self._zoom_btn)

        self._scroll = Gtk.ScrolledWindow()
        self._scroll.get_style_context().add_class('nsnip-canvas')
        self._scroll.connect('size-allocate', lambda *_: self._fit and GLib.idle_add(self._apply_zoom))
        self._da = Gtk.DrawingArea(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self._da.set_margin_start(16); self._da.set_margin_end(16)
        self._da.set_margin_top(16);   self._da.set_margin_bottom(16)
        self._da.set_events(
            Gdk.EventMask.BUTTON_PRESS_MASK |
            Gdk.EventMask.BUTTON_RELEASE_MASK |
            Gdk.EventMask.POINTER_MOTION_MASK |
            Gdk.EventMask.SCROLL_MASK |
            Gdk.EventMask.SMOOTH_SCROLL_MASK
        )
        self._da.connect('draw', self._on_draw)
        self._da.connect('button-press-event', self._on_press)
        self._da.connect('motion-notify-event', self._on_motion)
        self._da.connect('button-release-event', self._on_release)
        self._da.connect('scroll-event', self._on_scroll)
        self._da.connect('realize', lambda w: (self._im.set_client_window(w.get_window()),
                                               self._update_cursor()))
        self._scroll.add(self._da)

        self._crop_bar = Gtk.Revealer()
        bar = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        bar.set_margin_top(8); bar.set_margin_bottom(8)
        cancel = Gtk.Button(label='Cancel')
        cancel.connect('clicked', lambda _: self._leave_crop(apply=False))
        apply = Gtk.Button(label='Apply crop')
        apply.get_style_context().add_class('suggested-action')
        apply.connect('clicked', lambda _: self._leave_crop(apply=True))
        bar.pack_start(cancel, False, False, 0)
        bar.pack_start(apply, False, False, 0)
        self._crop_bar.add(bar)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        vbox.pack_start(self._scroll, True, True, 0)
        vbox.pack_start(self._crop_bar, False, False, 0)
        self.add(vbox)

    def _icon_button(self, icon: str, tip: str, cb) -> Gtk.Button:
        btn = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
        btn.set_relief(Gtk.ReliefStyle.NONE)
        btn.set_tooltip_text(tip)
        btn.connect('clicked', lambda _: cb())
        return btn

    def _popover_list(self, items: list[tuple[str, Any]]) -> Gtk.Popover:
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_margin_start(6); box.set_margin_end(6)
        box.set_margin_top(6);   box.set_margin_bottom(6)
        for label, cb in items:
            if label is None:
                box.pack_start(Gtk.Separator(), False, False, 4)
                continue
            btn = Gtk.ModelButton(text=label)
            btn.connect('clicked', lambda _b, f=cb: (pop.popdown(), f()))
            box.pack_start(btn, False, False, 0)
        box.show_all()
        pop.add(box)
        return pop

    def _build_new_popover(self) -> Gtk.Popover:
        items = [(f'{label} snip', lambda k=key: self._new_snip(mode=k)) for key, label in SNIP_MODES]
        items.append((None, None))
        items += [(f'Snip in {n} seconds', lambda n=n: self._new_snip(delay=n)) for n in (3, 5, 10)]
        return self._popover_list(items)

    def _build_menu_popover(self) -> Gtk.Popover:
        return self._popover_list([
            ('Open image…', self._open_file),
            ('Open screenshots folder', self._open_folder),
            (None, None),
            ('Settings', lambda: PreferencesDialog(self._app, self)),
        ])

    def _tool_popover(self, tool: str, relative_to: Gtk.Widget):
        st = self._settings[tool]
        pop = Gtk.Popover(relative_to=relative_to)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_start(12); box.set_margin_end(12)
        box.set_margin_top(12);   box.set_margin_bottom(12)

        def refresh():
            relative_to.queue_draw()
            pop.popdown()

        if tool == 'shape':
            kinds = Gtk.Box()
            kinds.get_style_context().add_class('linked')
            group = None
            for kind, label in _SHAPES:
                rb = Gtk.RadioButton.new_with_label_from_widget(group, label)
                group = group or rb
                rb.set_mode(False)
                rb.set_active(st['kind'] == kind)
                rb.connect('toggled', lambda b, k=kind: b.get_active() and st.update(kind=k))
                kinds.pack_start(rb, True, True, 0)
            box.pack_start(kinds, False, False, 0)

        grid = Gtk.Grid(column_spacing=4, row_spacing=4)
        palette = _HIGHLIGHTS if tool == 'highlighter' else _PALETTE
        for i, color in enumerate(palette):
            btn = Gtk.Button()
            btn.set_relief(Gtk.ReliefStyle.NONE)
            btn.add(_swatch(color, color == st['color']))
            btn.connect('clicked', lambda _b, c=color: (st.update(color=c), refresh()))
            grid.attach(btn, i % 6, i // 6, 1, 1)
        box.pack_start(grid, False, False, 0)

        def custom_color(_b):
            pop.popdown()
            dlg = Gtk.ColorChooserDialog(title='Custom colour', transient_for=self)
            rgba = Gdk.RGBA()
            rgba.red, rgba.green, rgba.blue, rgba.alpha = *st['color'], 1.0
            dlg.set_rgba(rgba)
            if dlg.run() == Gtk.ResponseType.OK:
                c = dlg.get_rgba()
                st['color'] = (c.red, c.green, c.blue)
                relative_to.queue_draw()
            dlg.destroy()

        more = Gtk.Button(label='More colours…')
        more.connect('clicked', custom_color)
        box.pack_start(more, False, False, 0)

        lo, hi = {'pen': (1, 30), 'highlighter': (6, 60), 'shape': (1, 30), 'text': (10, 120)}[tool]
        size_row = Gtk.Box(spacing=8)
        size_row.pack_start(Gtk.Label(label='Size'), False, False, 0)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, 1)
        scale.set_value(st['size'])
        scale.set_size_request(180, -1)
        scale.set_value_pos(Gtk.PositionType.RIGHT)
        scale.set_digits(0)
        scale.connect('value-changed', lambda s: st.update(size=s.get_value()))
        size_row.pack_start(scale, True, True, 0)
        box.pack_start(size_row, False, False, 0)

        box.show_all()
        pop.add(box)
        pop.popup()

    def _size_to_image(self):
        display = self.get_display()
        _s, px, py = display.get_default_seat().get_pointer().get_position()
        geom = display.get_monitor_at_point(px, py).get_workarea()
        w = min(max(self._image.width + 48, 760), int(geom.width * 0.85))
        h = min(max(self._image.height + 120, 480), int(geom.height * 0.85))
        self.set_default_size(w, h)
        self.set_position(Gtk.WindowPosition.CENTER)

    # ── Tools ────────────────────────────────────────────────────────────────

    def _on_tool_clicked(self, tool: str, btn: Gtk.Button):
        if tool == self._tool:
            if tool in self._settings:
                self._tool_popover(tool, btn)
            elif tool == 'crop':
                self._leave_crop(apply=True)
            return
        self._finish_text()
        if self._tool == 'crop':
            self._leave_crop(apply=True)
        if tool == 'crop':
            self._prev_tool = self._tool
            self._crop_edit = list(self._crop)
            self._crop_bar.set_reveal_child(True)
            self._apply_zoom()
        self._tool = tool
        self._update_tool_buttons()
        self._da.queue_draw()

    def _leave_crop(self, apply: bool):
        if self._tool != 'crop':
            return
        edit, self._crop_edit = self._crop_edit, None
        self._tool = self._prev_tool
        self._crop_bar.set_reveal_child(False)
        self._update_tool_buttons()
        if apply and tuple(edit) != self._crop:
            self._commit(list(self._anns), tuple(edit))
        else:
            self._apply_zoom()
            self._da.queue_draw()

    def _update_tool_buttons(self):
        for key, btn in self._tool_btns.items():
            ctx = btn.get_style_context()
            if key == self._tool:
                ctx.add_class('nsnip-active')
            else:
                ctx.remove_class('nsnip-active')
        self._update_cursor()

    def _update_cursor(self):
        gdk_win = self._da.get_window()
        if gdk_win is None:
            return
        name = {'text': 'text', 'eraser': 'cell', 'crop': 'default'}.get(self._tool, 'crosshair')
        gdk_win.set_cursor(Gdk.Cursor.new_from_name(self.get_display(), name))

    def _update_history_buttons(self):
        self._undo_btn.set_sensitive(bool(self._undo))
        self._redo_btn.set_sensitive(bool(self._redo))

    # ── Zoom & coordinates ───────────────────────────────────────────────────

    def _view(self) -> tuple:
        if self._crop_edit is not None:
            return 0, 0, self._image.width, self._image.height
        return self._crop

    def _apply_zoom(self):
        _vx, _vy, vw, vh = self._view()
        if self._fit:
            alloc = self._scroll.get_allocation()
            if alloc.width > 1:
                self._zoom = min(1.0, (alloc.width - 40) / vw, (alloc.height - 40) / vh)
                self._zoom = max(self._zoom, 0.05)
        w, h = max(1, round(vw * self._zoom)), max(1, round(vh * self._zoom))
        if tuple(self._da.get_size_request()) != (w, h):
            self._da.set_size_request(w, h)
        self._zoom_btn.set_label(f'{round(self._zoom * 100)}%')
        self._da.queue_draw()
        return GLib.SOURCE_REMOVE

    def _set_zoom(self, zoom: float):
        self._fit = False
        self._zoom = max(0.05, min(8.0, zoom))
        self._apply_zoom()

    def _toggle_fit(self):
        if self._fit:
            self._set_zoom(1.0)
        else:
            self._fit = True
            self._apply_zoom()

    def _to_img(self, event) -> tuple[float, float]:
        vx, vy, _w, _h = self._view()
        return event.x / self._zoom + vx, event.y / self._zoom + vy

    def _on_scroll(self, _w, event):
        if not event.state & Gdk.ModifierType.CONTROL_MASK:
            return False
        if event.direction == Gdk.ScrollDirection.SMOOTH:
            dy = event.get_scroll_deltas()[2]
        else:
            dy = -1 if event.direction == Gdk.ScrollDirection.UP else 1
        if dy:
            self._set_zoom(self._zoom * (1.15 ** -dy))
        return True

    # ── Drawing ──────────────────────────────────────────────────────────────

    def _on_draw(self, _w, cr: cairo.Context):
        vx, vy, vw, vh = self._view()
        z = self._zoom
        cr.scale(z, z)
        cr.translate(-vx, -vy)
        cr.rectangle(vx, vy, vw, vh)
        cr.clip()
        cr.set_source_surface(self._bg, 0, 0)
        cr.get_source().set_filter(cairo.Filter.GOOD)
        cr.paint()
        for ann in self._anns:
            _render_annotation(cr, ann)
        if self._current is not None:
            _render_annotation(cr, self._current)
        if self._text_edit is not None:
            _render_annotation(cr, self._text_edit, caret=True)

        if self._crop_edit is not None:
            cx, cy, cw, ch = self._crop_edit
            cr.rectangle(vx, vy, vw, vh)
            cr.rectangle(cx, cy, cw, ch)
            cr.set_fill_rule(cairo.FillRule.EVEN_ODD)
            cr.set_source_rgba(0, 0, 0, 0.5)
            cr.fill()
            cr.set_source_rgb(1, 1, 1)
            cr.set_line_width(1.5 / z)
            cr.rectangle(cx, cy, cw, ch)
            cr.stroke()
            for third in (1, 2):
                cr.move_to(cx + cw * third / 3, cy); cr.rel_line_to(0, ch)
                cr.move_to(cx, cy + ch * third / 3); cr.rel_line_to(cw, 0)
            cr.set_source_rgba(1, 1, 1, 0.35)
            cr.set_line_width(1 / z)
            cr.stroke()
            r = 5 / z
            for hx, hy in self._handle_points().values():
                cr.rectangle(hx - r, hy - r, 2 * r, 2 * r)
            cr.set_source_rgb(1, 1, 1)
            cr.fill_preserve()
            cr.set_source_rgba(0, 0, 0, 0.6)
            cr.set_line_width(1 / z)
            cr.stroke()

    def _handle_points(self) -> dict:
        x, y, w, h = self._crop_edit
        xs = {'w': x, 'e': x + w}
        ys = {'n': y, 's': y + h}
        return {
            name: (next((xs[c] for c in name if c in xs), x + w / 2),
                   next((ys[c] for c in name if c in ys), y + h / 2))
            for name in _HANDLES
        }

    # ── Pointer ──────────────────────────────────────────────────────────────

    def _on_press(self, _w, event):
        if event.button != 1:
            return
        x, y = self._to_img(event)
        tool = self._tool

        if tool == 'crop':
            r = 10 / self._zoom
            for name, (hx, hy) in self._handle_points().items():
                if abs(x - hx) <= r and abs(y - hy) <= r:
                    self._crop_drag = (name, x, y, tuple(self._crop_edit))
                    return
            self._crop_drag = ('move' if _contains(self._crop_edit, x, y) else 'new',
                               x, y, tuple(self._crop_edit))
            return

        self._finish_text()
        if tool == 'text':
            st = self._settings['text']
            self._text_edit = TextMark(x, y - st['size'] / 2, '', st['color'], st['size'])
            self._im.focus_in()
            self._im.reset()
        elif tool == 'eraser':
            self._erase_snapshot = list(self._anns)
            self._erase_at(x, y)
        elif tool in ('pen', 'highlighter'):
            st = self._settings[tool]
            self._current = Stroke([(x, y)], st['color'], st['size'], tool == 'highlighter')
        elif tool == 'shape':
            st = self._settings['shape']
            self._current = Shape(st['kind'], x, y, x, y, st['color'], st['size'])
        self._da.queue_draw()

    def _on_motion(self, _w, event):
        x, y = self._to_img(event)
        if self._tool == 'crop':
            if self._crop_drag and event.state & Gdk.ModifierType.BUTTON1_MASK:
                self._drag_crop(x, y)
            return
        if self._erase_snapshot is not None:
            self._erase_at(x, y)
        elif isinstance(self._current, Stroke):
            lx, ly = self._current.points[-1]
            if math.hypot(x - lx, y - ly) >= 1 / self._zoom:
                self._current.points.append((x, y))
                self._da.queue_draw()
        elif isinstance(self._current, Shape):
            self._current.x2, self._current.y2 = x, y
            self._da.queue_draw()

    def _on_release(self, _w, event):
        if event.button != 1:
            return
        if self._tool == 'crop':
            self._crop_drag = None
            return
        if self._erase_snapshot is not None:
            snapshot, self._erase_snapshot = self._erase_snapshot, None
            if len(snapshot) != len(self._anns):
                current, self._anns = self._anns, snapshot
                self._commit(current)
        elif self._current is not None:
            ann, self._current = self._current, None
            if not (isinstance(ann, Shape) and ann.x1 == ann.x2 and ann.y1 == ann.y2):
                self._commit(self._anns + [ann])
            self._da.queue_draw()

    def _erase_at(self, x, y):
        r = 6 / self._zoom
        for ann in reversed(self._anns):
            if _hit(ann, x, y, r):
                self._anns = [a for a in self._anns if a is not ann]
                self._da.queue_draw()
                return

    def _drag_crop(self, x, y):
        kind, sx, sy, (ox, oy, ow, oh) = self._crop_drag
        iw, ih = self._image.width, self._image.height
        dx, dy = x - sx, y - sy
        if kind == 'move':
            nx = max(0, min(iw - ow, ox + dx))
            ny = max(0, min(ih - oh, oy + dy))
            self._crop_edit = [round(nx), round(ny), ow, oh]
        elif kind == 'new':
            x1, y1 = max(0, min(iw, sx)), max(0, min(ih, sy))
            x2, y2 = max(0, min(iw, x)), max(0, min(ih, y))
            nx, ny, nw, nh = _norm_rect(x1, y1, x2, y2)
            if nw >= 8 and nh >= 8:
                self._crop_edit = [round(nx), round(ny), round(nw), round(nh)]
        else:
            x1, y1, x2, y2 = ox, oy, ox + ow, oy + oh
            if 'w' in kind: x1 = max(0, min(x2 - 8, ox + dx))
            if 'e' in kind: x2 = min(iw, max(x1 + 8, ox + ow + dx))
            if 'n' in kind: y1 = max(0, min(y2 - 8, oy + dy))
            if 's' in kind: y2 = min(ih, max(y1 + 8, oy + oh + dy))
            self._crop_edit = [round(x1), round(y1), round(x2 - x1), round(y2 - y1)]
        self._da.queue_draw()

    # ── Text ─────────────────────────────────────────────────────────────────

    def _on_im_commit(self, _im, text: str):
        if self._text_edit is not None:
            self._text_edit.text += text
            self._da.queue_draw()

    def _finish_text(self):
        mark, self._text_edit = self._text_edit, None
        if mark is None:
            return
        self._im.focus_out()
        if mark.text.strip():
            self._commit(self._anns + [mark])
        self._da.queue_draw()

    # ── Keyboard ─────────────────────────────────────────────────────────────

    def _on_key(self, _w, event):
        key = event.keyval
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)

        if self._text_edit is not None and not ctrl:
            if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                self._finish_text()
            elif key == Gdk.KEY_Escape:
                self._text_edit = None
                self._im.focus_out()
                self._da.queue_draw()
            elif key == Gdk.KEY_BackSpace:
                self._text_edit.text = self._text_edit.text[:-1]
                self._da.queue_draw()
            else:
                self._im.filter_keypress(event)
            return True

        if self._tool == 'crop' and key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Escape):
            self._leave_crop(apply=key != Gdk.KEY_Escape)
            return True

        if not ctrl:
            return False
        key = Gdk.keyval_to_lower(key)
        actions = {
            Gdk.KEY_z: self._redo_action if shift else self._undo_action,
            Gdk.KEY_y: self._redo_action,
            Gdk.KEY_c: self._copy,
            Gdk.KEY_s: self._save_as,
            Gdk.KEY_n: self._new_snip,
            Gdk.KEY_o: self._open_file,
            Gdk.KEY_0: lambda: self._toggle_fit() if not self._fit else None,
            Gdk.KEY_1: lambda: self._set_zoom(1.0),
            Gdk.KEY_plus: lambda: self._set_zoom(self._zoom * 1.25),
            Gdk.KEY_equal: lambda: self._set_zoom(self._zoom * 1.25),
            Gdk.KEY_minus: lambda: self._set_zoom(self._zoom / 1.25),
        }
        if key in actions:
            actions[key]()
            return True
        return False

    # ── Actions ──────────────────────────────────────────────────────────────

    def _copy(self):
        copy_to_clipboard(self.flatten())

    def _save_as(self):
        dlg = Gtk.FileChooserNative(
            title='Save As', transient_for=self, action=Gtk.FileChooserAction.SAVE,
        )
        dlg.set_do_overwrite_confirmation(True)
        folder = _save_dir(self._app.cfg)
        os.makedirs(folder, exist_ok=True)
        dlg.set_current_folder(folder)
        dlg.set_current_name(_timestamp_name())
        for name, pattern in (('PNG image', '*.png'), ('JPEG image', '*.jpg')):
            f = Gtk.FileFilter()
            f.set_name(name)
            f.add_pattern(pattern)
            dlg.add_filter(f)
        if dlg.run() == Gtk.ResponseType.ACCEPT:
            path = dlg.get_filename()
            if not os.path.splitext(path)[1]:
                path += '.jpg' if dlg.get_filter().get_name().startswith('JPEG') else '.png'
            try:
                self.flatten().save(path)
            except (OSError, ValueError) as e:
                _show_info('Could not save image', str(e))
        dlg.destroy()

    def _open_file(self):
        dlg = Gtk.FileChooserNative(title='Open image', transient_for=self,
                                    action=Gtk.FileChooserAction.OPEN)
        f = Gtk.FileFilter()
        f.set_name('Images')
        f.add_pixbuf_formats()
        dlg.add_filter(f)
        if dlg.run() == Gtk.ResponseType.ACCEPT:
            path = dlg.get_filename()
            try:
                self.load_image(Image.open(path), path)
            except OSError as e:
                _show_info('Could not open image', str(e))
        dlg.destroy()

    def _open_folder(self):
        folder = _save_dir(self._app.cfg)
        os.makedirs(folder, exist_ok=True)
        Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(folder), None)

    def _new_snip(self, mode: str | None = None, delay: int = 0):
        self._finish_text()
        self._app.start_snip(mode=mode, delay=delay, editor=self)


# ── System tray ───────────────────────────────────────────────────────────────

def _render_icon(size: int, path: str):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    cr.scale(size / 22, size / 22)

    cr.set_source_rgba(0.18, 0.54, 0.92, 1.0)
    cr.arc(11, 11, 11, 0, 2 * math.pi)
    cr.fill()

    cr.set_source_rgba(1, 1, 1, 0.95)
    cr.set_line_width(1.8)
    cr.set_line_cap(cairo.LineCap.ROUND)
    m, gap = 11, 5
    cr.move_to(m, 2);       cr.line_to(m, m - gap)
    cr.move_to(m, m + gap); cr.line_to(m, 20)
    cr.move_to(2, m);       cr.line_to(m - gap, m)
    cr.move_to(m + gap, m); cr.line_to(20, m)
    cr.stroke()
    cr.arc(m, m, gap - 1, 0, 2 * math.pi)
    cr.stroke()

    os.makedirs(os.path.dirname(path), exist_ok=True)
    surface.write_to_png(path)


def _make_tray_icon() -> tuple[str, str]:
    """
    Write the icon into a proper XDG theme directory structure so that
    AppIndicator can find it by name.  Returns (theme_dir, icon_name).
    """
    theme_dir = os.path.join(tempfile.gettempdir(), 'nsnip-icons')
    _render_icon(22, os.path.join(theme_dir, 'hicolor', '22x22', 'apps', 'nsnip.png'))
    return theme_dir, 'nsnip'


class TrayIcon:
    def __init__(self, app: 'NSnipApp'):
        self._app = app
        menu = self._build_menu()
        theme_dir, icon_name = _make_tray_icon()

        if self._try_appindicator(theme_dir, icon_name, menu):
            return

        # Fallback: Gtk.StatusIcon (works on XFCE, KDE, MATE, etc.)
        icon_path = os.path.join(theme_dir, 'hicolor', '22x22', 'apps', 'nsnip.png')
        icon = Gtk.StatusIcon()
        icon.set_from_file(icon_path)
        icon.set_tooltip_text('NSnip — click to snip')
        icon.connect('activate', lambda _: app.start_snip(settle_ms=200))
        icon.connect('popup-menu', self._status_icon_popup, menu)
        self._icon = icon  # keep alive

    def _try_appindicator(self, theme_dir: str, icon_name: str, menu: Gtk.Menu) -> bool:
        for ns in ('AyatanaAppIndicator3', 'AppIndicator3'):
            try:
                gi.require_version(ns, '0.1')
                AI = importlib.import_module(f'gi.repository.{ns}')
                ind = AI.Indicator.new(
                    'nsnip', icon_name,
                    AI.IndicatorCategory.APPLICATION_STATUS
                )
                ind.set_icon_theme_path(theme_dir)
                # Menu must be set before ACTIVE or the library won't register
                ind.set_menu(menu)
                ind.set_secondary_activate_target(self._snip_item)
                ind.set_status(AI.IndicatorStatus.ACTIVE)
                self._indicator = ind  # keep alive
                return True
            except Exception:
                continue
        return False

    def _build_menu(self) -> Gtk.Menu:
        app = self._app
        menu = Gtk.Menu()

        def item(label: str, cb):
            mi = Gtk.MenuItem(label=label)
            mi.connect('activate', lambda _: cb())
            menu.append(mi)
            return mi

        # Give the menu time to close so it is not in the screenshot
        self._snip_item = item('New snip', lambda: app.start_snip(settle_ms=300))
        for n in (3, 5, 10):
            item(f'New snip in {n} seconds', lambda n=n: app.start_snip(delay=n))
        menu.append(Gtk.SeparatorMenuItem())
        item('Open image…', app.open_file)
        item('Settings', lambda: PreferencesDialog(app))
        menu.append(Gtk.SeparatorMenuItem())
        item('Quit', app.quit)

        menu.show_all()
        return menu

    def _status_icon_popup(self, icon, button, time, menu):
        menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, time)


# ── Application ───────────────────────────────────────────────────────────────

def _arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='nsnip',
        description='Windows-style snipping tool. With no options, runs in the tray.',
    )
    p.add_argument('--snip', '--once', '-o', action='store_true',
                   help='take a snip now (instant when NSnip is already running)')
    p.add_argument('--mode', choices=[m for m, _ in SNIP_MODES],
                   help='snipping mode (default: the last one used)')
    p.add_argument('--delay', type=int, default=0, metavar='SECONDS',
                   help='wait before snipping')
    p.add_argument('--edit', metavar='IMAGE', help='open an image in the editor')
    return p


class NSnipApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.cfg: dict = {}
        self._daemon = False
        self._tray: TrayIcon | None = None
        self._listener = None
        self._session: SnipSession | str | None = None  # 'pending' while a delay runs
        self._snips: dict[int, tuple[Image.Image, str | None]] = {}
        self._next_snip = 0

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.cfg = _load_config()
        _install_css()

        def action(name, cb, ptype=None):
            act = Gio.SimpleAction.new(name, ptype)
            act.connect('activate', cb)
            self.add_action(act)

        action('snip', lambda *_: self.start_snip())
        action('edit', lambda _a, p: self._edit_snip(p.get_uint32()), GLib.VariantType('u'))

    def do_command_line(self, cmdline):
        args = _arg_parser().parse_args(cmdline.get_arguments()[1:])
        if args.edit:
            path = cmdline.create_file_for_arg(args.edit).get_path()
            try:
                self.open_editor(Image.open(path), path)
            except OSError as e:
                cmdline.printerr(f'nsnip: {e}\n')
                return 1
        elif args.snip:
            self.start_snip(mode=args.mode, delay=args.delay)
        elif not self._daemon:
            self._start_daemon()
        else:
            self.start_snip()
        return 0

    def do_shutdown(self):
        if self._listener is not None:
            self._listener.stop()
        Gtk.Application.do_shutdown(self)

    # ── Tray mode ────────────────────────────────────────────────────────────

    def _start_daemon(self):
        self._daemon = True
        self.hold()
        self.apply_settings()
        self._tray = TrayIcon(self)
        print(f'NSnip running. Shortcut: {_accel_label(self.cfg["hotkey"])}')

    def apply_settings(self):
        if apply_shortcuts(self.cfg) or not self._daemon or WAYLAND:
            return
        try:
            from pynput import keyboard
        except ImportError:
            print('pynput not found: no global shortcut. Run: pip install pynput')
            return
        hotkey = _accel_to_pynput(self.cfg['hotkey'])
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        if hotkey:
            self._listener = keyboard.GlobalHotKeys(
                {hotkey: lambda: GLib.idle_add(self.start_snip)}
            )
            self._listener.start()

    # ── Snipping ─────────────────────────────────────────────────────────────

    def start_snip(self, mode: str | None = None, delay: int = 0,
                   editor: EditorWindow | None = None, settle_ms: int = 0):
        if self._session is not None:
            return
        self.hold()
        if editor is not None:
            editor.hide()
            settle_ms = max(settle_ms, 250)

        def begin():
            try:
                self._session = SnipSession(
                    self, mode or self.cfg['mode'],
                    lambda img: self._on_snip_done(img, editor),
                )
            except Exception as e:
                self._session = None
                _show_info('Could not take a screenshot', str(e))
                if editor is not None:
                    editor.present()
                self.release()
            return GLib.SOURCE_REMOVE

        self._session = 'pending'  # block re-entry during the delay
        GLib.timeout_add(delay * 1000 + settle_ms, begin)

    def _on_snip_done(self, image: Image.Image | None, editor: EditorWindow | None):
        self._session = None
        if image is not None:
            copy_to_clipboard(image, store=True)
            path = autosave(image, self.cfg) if self.cfg['auto_save'] else None
            if editor is not None:
                editor.load_image(image, path)
            else:
                self._notify(image, path)
        if editor is not None:
            editor.present()

        if self._daemon:
            self.release()
        else:
            # One-shot: stay around long enough for the notification to be clicked
            GLib.timeout_add_seconds(15, self.release)

    def _notify(self, image: Image.Image, path: str | None):
        sid = self._next_snip
        self._next_snip += 1
        self._snips[sid] = (image, path)
        self._snips.pop(sid - 10, None)

        if path is None:
            path = os.path.join(GLib.get_user_cache_dir(), 'nsnip', 'last-snip.png')
            os.makedirs(os.path.dirname(path), exist_ok=True)
            thumb = image.copy()
            thumb.thumbnail((256, 256))
            thumb.save(path)

        n = Gio.Notification.new('Snip copied to clipboard')
        n.set_body('Select here to mark up and share the image.')
        n.set_icon(Gio.FileIcon.new(Gio.File.new_for_path(path)))
        n.set_default_action_and_target('app.edit', GLib.Variant('u', sid))
        self.send_notification('snip', n)

    def _edit_snip(self, sid: int):
        if sid in self._snips:
            self.withdraw_notification('snip')
            self.open_editor(*self._snips[sid])

    def open_editor(self, image: Image.Image, path: str | None = None):
        EditorWindow(self, image, path).show_all()

    def open_file(self):
        dlg = Gtk.FileChooserNative(title='Open image', action=Gtk.FileChooserAction.OPEN)
        f = Gtk.FileFilter()
        f.set_name('Images')
        f.add_pixbuf_formats()
        dlg.add_filter(f)
        if dlg.run() == Gtk.ResponseType.ACCEPT:
            path = dlg.get_filename()
            try:
                self.open_editor(Image.open(path), path)
            except OSError as e:
                _show_info('Could not open image', str(e))
        dlg.destroy()


# ── Setup ─────────────────────────────────────────────────────────────────────

def setup():
    """nsnip-setup: install icon, launcher, autostart entry and shortcut."""
    icons = os.path.expanduser('~/.local/share/icons/hicolor')
    for size in (22, 48, 128):
        _render_icon(size, os.path.join(icons, f'{size}x{size}', 'apps', 'nsnip.png'))
    subprocess.run(['gtk-update-icon-cache', '-f', '-t', icons], stderr=subprocess.DEVNULL)

    apps_dir = os.path.expanduser('~/.local/share/applications')
    os.makedirs(apps_dir, exist_ok=True)
    desktop_path = os.path.join(apps_dir, 'nsnip.desktop')
    with open(desktop_path, 'w') as f:
        f.write(_desktop_entry())

    if not os.path.exists(_AUTOSTART_FILE):
        _set_autostart(True)

    cfg = _load_config()
    _save_config(cfg)
    registered = apply_shortcuts(cfg)

    print(f'Icon      → {icons}')
    print(f'Launcher  → {desktop_path}')
    print(f'Autostart → {_AUTOSTART_FILE} ({"on" if _autostart_enabled() else "off"})')
    if registered:
        print(f'Shortcut  → {_accel_label(cfg["hotkey"])}')
    else:
        print('Shortcut  → not registered: bind `nsnip --snip` in your desktop settings')
    print('Done. Start NSnip from your app menu or run: nsnip')


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    _arg_parser().parse_args()  # handles --help and bad arguments locally
    sys.exit(NSnipApp().run(sys.argv))


if __name__ == '__main__':
    main()
