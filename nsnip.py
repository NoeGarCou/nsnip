#!/usr/bin/env python3
"""NSnip — freeze-first screenshot tool with annotations and system tray."""

import importlib.metadata
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import urllib.request
from dataclasses import dataclass
from typing import Any

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib
import cairo
from PIL import Image

WAYLAND = 'wayland' in os.environ.get('XDG_SESSION_TYPE', '').lower()
_GITHUB_REPO = 'NoeGarCou/nsnip'


# ── Annotation types ──────────────────────────────────────────────────────────

@dataclass
class Stroke:
    points: list
    color: tuple
    width: float

@dataclass
class Rect:
    x1: int; y1: int; x2: int; y2: int
    color: tuple
    width: float

@dataclass
class Arrow:
    x1: int; y1: int; x2: int; y2: int
    color: tuple
    width: float

@dataclass
class TextMark:
    x: int; y: int
    text: str
    color: tuple
    size: float


# ── Screen capture ────────────────────────────────────────────────────────────

def capture_all_screens() -> tuple[Image.Image, dict]:
    try:
        import mss
    except ImportError:
        sys.exit('mss not found. Run: pip install mss')
    with mss.MSS() as sct:
        bbox = sct.monitors[0]
        raw = sct.grab(bbox)
        img = Image.frombytes('RGB', raw.size, raw.bgra, 'raw', 'BGRX')
        return img, bbox


# ── Clipboard ─────────────────────────────────────────────────────────────────

def copy_to_clipboard(image: Image.Image):
    buf = io.BytesIO()
    image.save(buf, format='PNG')
    buf.seek(0)
    loader = GdkPixbuf.PixbufLoader.new_with_type('png')
    loader.write(buf.read())
    loader.close()
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    clipboard.set_image(loader.get_pixbuf())
    clipboard.store()


# ── Cairo helpers ─────────────────────────────────────────────────────────────

def _pil_to_surface(image: Image.Image) -> cairo.ImageSurface:
    buf = io.BytesIO()
    image.save(buf, format='PNG')
    buf.seek(0)
    return cairo.ImageSurface.create_from_png(buf)


def _render_annotation(cr: cairo.Context, ann: Any):
    cr.set_source_rgb(*ann.color)
    cr.set_line_cap(cairo.LineCap.ROUND)
    cr.set_line_join(cairo.LineJoin.ROUND)

    if isinstance(ann, Stroke):
        if len(ann.points) < 2:
            return
        cr.set_line_width(ann.width)
        cr.move_to(*ann.points[0])
        for pt in ann.points[1:]:
            cr.line_to(*pt)
        cr.stroke()

    elif isinstance(ann, Rect):
        cr.set_line_width(ann.width)
        x = min(ann.x1, ann.x2)
        y = min(ann.y1, ann.y2)
        cr.rectangle(x, y, abs(ann.x2 - ann.x1), abs(ann.y2 - ann.y1))
        cr.stroke()

    elif isinstance(ann, Arrow):
        cr.set_line_width(ann.width)
        cr.move_to(ann.x1, ann.y1)
        cr.line_to(ann.x2, ann.y2)
        cr.stroke()
        angle = math.atan2(ann.y2 - ann.y1, ann.x2 - ann.x1)
        head = max(12.0, ann.width * 4)
        spread = math.pi / 6
        for side in (-spread, spread):
            cr.move_to(ann.x2, ann.y2)
            cr.line_to(
                ann.x2 - head * math.cos(angle - side),
                ann.y2 - head * math.sin(angle - side),
            )
            cr.stroke()

    elif isinstance(ann, TextMark):
        cr.set_font_size(ann.size)
        cr.move_to(ann.x, ann.y)
        cr.show_text(ann.text)


# ── Config ────────────────────────────────────────────────────────────────────

_CONFIG_DIR  = os.path.expanduser('~/.config/nsnip')
_CONFIG_FILE = os.path.join(_CONFIG_DIR, 'config.json')
_DEFAULTS: dict = {'hotkey': '<print_screen>'}


def _load_config() -> dict:
    try:
        with open(_CONFIG_FILE) as f:
            return {**_DEFAULTS, **json.load(f)}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_DEFAULTS)


def _save_config(cfg: dict):
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    with open(_CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)


# ── Hotkey helpers ────────────────────────────────────────────────────────────

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
    'Return': 'enter', 'KP_Enter': 'enter', 'space': 'space',
    'Menu': 'menu',
}

_LABEL: dict[str, str] = {
    'print_screen': 'Print Screen', 'scroll_lock': 'Scroll Lock',
    'num_lock': 'Num Lock',         'pause': 'Pause',
    'insert': 'Insert',             'delete': 'Delete',
    'home': 'Home',                 'end': 'End',
    'page_up': 'Page Up',           'page_down': 'Page Down',
    'up': '↑', 'down': '↓', 'left': '←', 'right': '→',
    **{f'f{i}': f'F{i}' for i in range(1, 13)},
    'esc': 'Esc', 'tab': 'Tab', 'backspace': 'Backspace',
    'enter': 'Enter', 'space': 'Space', 'menu': 'Menu',
    'ctrl': 'Ctrl', 'shift': 'Shift', 'alt': 'Alt', 'super': 'Super',
}


def _event_to_pynput(event) -> str | None:
    """Convert a GTK key-press event to a pynput hotkey string, or None for bare modifiers."""
    if event.keyval in _MODIFIER_KEYSYMS:
        return None

    mask = event.state & ~(Gdk.ModifierType.MOD2_MASK | Gdk.ModifierType.LOCK_MASK)
    parts = []
    if mask & Gdk.ModifierType.CONTROL_MASK: parts.append('<ctrl>')
    if mask & Gdk.ModifierType.MOD1_MASK:    parts.append('<alt>')
    if mask & Gdk.ModifierType.SUPER_MASK:   parts.append('<super>')
    if mask & Gdk.ModifierType.SHIFT_MASK:   parts.append('<shift>')

    key_name = Gdk.keyval_name(event.keyval)
    pynput = _GDK_TO_PYNPUT.get(key_name)
    if pynput:
        parts.append(f'<{pynput}>')
    elif len(key_name) == 1:
        parts.append(key_name.lower())
    else:
        return None

    return '+'.join(parts)


def _pynput_to_label(hotkey: str) -> str:
    labels = []
    for part in hotkey.split('+'):
        if part.startswith('<') and part.endswith('>'):
            name = part[1:-1]
            labels.append(_LABEL.get(name, name.title()))
        else:
            labels.append(part.upper() if len(part) == 1 else part)
    return '+'.join(labels)


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
    import shutil
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


# ── Preferences dialog ────────────────────────────────────────────────────────

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
        result = _event_to_pynput(event)
        if result:
            self.captured = result
            self.response(Gtk.ResponseType.OK)


class PreferencesDialog(Gtk.Dialog):
    def __init__(self, cfg: dict, on_save):
        super().__init__(title='NSnip Preferences', transient_for=None, modal=False)
        self.set_default_size(340, 110)
        self._cfg = dict(cfg)
        self._hotkey = cfg['hotkey']

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        grid.set_margin_start(16); grid.set_margin_end(16)
        grid.set_margin_top(14);   grid.set_margin_bottom(14)

        grid.attach(Gtk.Label(label='Screenshot shortcut:', halign=Gtk.Align.START), 0, 0, 1, 1)

        self._key_btn = Gtk.Button(label=_pynput_to_label(self._hotkey))
        self._key_btn.set_tooltip_text('Click to change the shortcut')
        self._key_btn.connect('clicked', self._on_change_key)
        grid.attach(self._key_btn, 1, 0, 1, 1)

        self.get_content_area().add(grid)

        self.add_button('Cancel', Gtk.ResponseType.CANCEL)
        save_btn = self.add_button('Save', Gtk.ResponseType.OK)
        save_btn.get_style_context().add_class('suggested-action')

        self.show_all()
        if self.run() == Gtk.ResponseType.OK:
            self._cfg['hotkey'] = self._hotkey
            _save_config(self._cfg)
            on_save(self._hotkey)
        self.destroy()

    def _on_change_key(self, _btn):
        cap = _HotkeyCaptureDialog(self)
        if cap.captured:
            self._hotkey = cap.captured
            self._key_btn.set_label(_pynput_to_label(cap.captured))


# ── Annotation window ─────────────────────────────────────────────────────────

class AnnotationWindow(Gtk.Window):
    def __init__(self, image: Image.Image):
        super().__init__(title='NSnip — Annotate')
        self.image = image
        self.result: Image.Image | None = None

        self._annotations: list = []
        self._current: Any = None
        self._tool = 'pen'
        self._color = (1.0, 0.2, 0.2)
        self._width = 3.0

        self._bg = _pil_to_surface(image)

        display = Gdk.Display.get_default()
        mon = display.get_primary_monitor()
        geom = mon.get_geometry()
        init_w = min(image.width, int(geom.width * 0.9))
        init_h = min(image.height, int(geom.height * 0.9) - 60)
        self.set_default_size(init_w, init_h + 50)
        self.set_position(Gtk.WindowPosition.CENTER)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(vbox)
        vbox.pack_start(self._build_toolbar(), False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._da = Gtk.DrawingArea()
        self._da.set_size_request(image.width, image.height)
        self._da.set_events(
            Gdk.EventMask.BUTTON_PRESS_MASK |
            Gdk.EventMask.BUTTON_RELEASE_MASK |
            Gdk.EventMask.POINTER_MOTION_MASK
        )
        self._da.connect('draw', self._on_draw)
        self._da.connect('button-press-event', self._on_press)
        self._da.connect('motion-notify-event', self._on_motion)
        self._da.connect('button-release-event', self._on_release)
        scroll.add(self._da)
        vbox.pack_start(scroll, True, True, 0)

        self.connect('key-press-event', self._on_key)

    # ── Toolbar ───────────────────────────────────────────────────────────────

    def _build_toolbar(self) -> Gtk.Widget:
        bar = Gtk.Box(spacing=4)
        bar.set_margin_start(6); bar.set_margin_end(6)
        bar.set_margin_top(4);   bar.set_margin_bottom(4)

        self._tool_btns: dict[str, Gtk.ToggleButton] = {}
        for key, label, icon in [
            ('pen',   'Pen',   'document-edit'),
            ('rect',  'Rect',  'draw-rectangle'),
            ('arrow', 'Arrow', 'media-seek-forward'),
            ('text',  'Text',  'insert-text'),
        ]:
            btn = Gtk.ToggleButton()
            btn.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.SMALL_TOOLBAR))
            btn.set_tooltip_text(label)
            btn.connect('toggled', self._on_tool_toggle, key)
            self._tool_btns[key] = btn
            bar.pack_start(btn, False, False, 0)

        self._tool_btns['pen'].set_active(True)

        bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 4)

        rgba = Gdk.RGBA()
        rgba.red, rgba.green, rgba.blue, rgba.alpha = *self._color, 1.0
        self._color_btn = Gtk.ColorButton()
        self._color_btn.set_rgba(rgba)
        self._color_btn.set_tooltip_text('Color')
        self._color_btn.connect('color-set', self._on_color_change)
        bar.pack_start(self._color_btn, False, False, 0)

        bar.pack_start(Gtk.Label(label=' Width:'), False, False, 0)
        adj = Gtk.Adjustment(value=3, lower=1, upper=30, step_increment=1)
        self._width_spin = Gtk.SpinButton(adjustment=adj, digits=0)
        self._width_spin.set_tooltip_text('Stroke width  (text: width × 6 = font size)')
        self._width_spin.connect('value-changed',
                                  lambda s: setattr(self, '_width', s.get_value()))
        bar.pack_start(self._width_spin, False, False, 0)

        bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 4)

        undo = Gtk.Button()
        undo.set_image(Gtk.Image.new_from_icon_name('edit-undo', Gtk.IconSize.SMALL_TOOLBAR))
        undo.set_tooltip_text('Undo  Ctrl+Z')
        undo.connect('clicked', lambda _: self._undo())
        bar.pack_start(undo, False, False, 0)

        bar.pack_start(Gtk.Box(), True, True, 0)  # spacer

        cancel = Gtk.Button(label='Cancel')
        cancel.connect('clicked', lambda _: self.destroy())
        bar.pack_end(cancel, False, False, 2)

        copy_btn = Gtk.Button(label='Copy & Close')
        copy_btn.get_style_context().add_class('suggested-action')
        copy_btn.set_tooltip_text('Ctrl+Return')
        copy_btn.connect('clicked', self._on_confirm)
        bar.pack_end(copy_btn, False, False, 0)

        return bar

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_tool_toggle(self, btn: Gtk.ToggleButton, key: str):
        if btn.get_active():
            self._tool = key
            for k, b in self._tool_btns.items():
                if k != key:
                    b.handler_block_by_func(self._on_tool_toggle)
                    b.set_active(False)
                    b.handler_unblock_by_func(self._on_tool_toggle)

    def _on_color_change(self, btn: Gtk.ColorButton):
        rgba = btn.get_rgba()
        self._color = (rgba.red, rgba.green, rgba.blue)

    def _on_key(self, _win, event):
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        if event.keyval == Gdk.KEY_z and ctrl:
            self._undo()
        elif event.keyval == Gdk.KEY_Return and ctrl:
            self._on_confirm()
        elif event.keyval == Gdk.KEY_Escape:
            self.destroy()

    def _on_draw(self, _widget, cr: cairo.Context):
        cr.set_source_surface(self._bg, 0, 0)
        cr.paint()
        for ann in self._annotations:
            _render_annotation(cr, ann)
        if self._current is not None:
            _render_annotation(cr, self._current)

    def _on_press(self, _widget, event):
        x, y = int(event.x), int(event.y)
        c, w = self._color, self._width
        if self._tool == 'pen':
            self._current = Stroke([(x, y)], c, w)
        elif self._tool == 'rect':
            self._current = Rect(x, y, x, y, c, w)
        elif self._tool == 'arrow':
            self._current = Arrow(x, y, x, y, c, w)
        elif self._tool == 'text':
            self._prompt_text(x, y)

    def _on_motion(self, widget, event):
        if self._current is None:
            return
        x, y = int(event.x), int(event.y)
        if isinstance(self._current, Stroke):
            self._current.points.append((x, y))
        elif isinstance(self._current, (Rect, Arrow)):
            self._current.x2, self._current.y2 = x, y
        widget.queue_draw()

    def _on_release(self, _widget, _event):
        if self._current is not None:
            self._annotations.append(self._current)
            self._current = None
            self._da.queue_draw()

    def _prompt_text(self, x: int, y: int):
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text='Enter text',
        )
        entry = Gtk.Entry()
        entry.set_activates_default(True)
        entry.set_margin_start(8); entry.set_margin_end(8); entry.set_margin_bottom(8)
        dlg.get_message_area().add(entry)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            text = entry.get_text().strip()
            if text:
                self._annotations.append(
                    TextMark(x, y, text, self._color, max(14.0, self._width * 6))
                )
                self._da.queue_draw()
        dlg.destroy()

    def _undo(self):
        if self._annotations:
            self._annotations.pop()
            self._da.queue_draw()

    # ── Flatten ───────────────────────────────────────────────────────────────

    def _on_confirm(self, _=None):
        w, h = self.image.width, self.image.height
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(surface)
        cr.set_source_surface(self._bg, 0, 0)
        cr.paint()
        for ann in self._annotations:
            _render_annotation(cr, ann)
        surface.flush()
        # Cairo ARGB32 in memory (little-endian) = BGRA byte order
        self.result = Image.frombuffer(
            'RGBA', (w, h), bytes(surface.get_data()), 'raw', 'BGRA'
        ).convert('RGB')
        self.destroy()


# ── Selection overlay ─────────────────────────────────────────────────────────

class SnipWindow(Gtk.Window):
    def __init__(self, screenshot: Image.Image, bbox: dict):
        super().__init__()
        self.screenshot = screenshot
        self.result: Image.Image | None = None

        self._sx = self._sy = 0
        self._cx = self._cy = 0
        self._active = False
        self._surface = _pil_to_surface(screenshot)

        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_app_paintable(True)
        self.move(bbox['left'], bbox['top'])
        self.resize(bbox['width'], bbox['height'])

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
        self.add(da)

        self.connect('key-press-event', self._on_key)
        self.connect('realize', lambda w: w.get_window().set_cursor(
            Gdk.Cursor.new_from_name(w.get_display(), 'crosshair')
        ))

    def _on_draw(self, _widget, cr: cairo.Context):
        cr.set_source_surface(self._surface, 0, 0)
        cr.paint()
        cr.set_source_rgba(0, 0, 0, 0.4)
        cr.paint()

        if self._active:
            x1 = min(self._sx, self._cx);  y1 = min(self._sy, self._cy)
            x2 = max(self._sx, self._cx);  y2 = max(self._sy, self._cy)
            w, h = x2 - x1, y2 - y1
            if w > 0 and h > 0:
                cr.save()
                cr.set_source_surface(self._surface, 0, 0)
                cr.rectangle(x1, y1, w, h)
                cr.fill()
                cr.restore()

                cr.set_source_rgb(1, 1, 1)
                cr.set_line_width(1.5)
                cr.set_dash([6, 3])
                cr.rectangle(x1, y1, w, h)
                cr.stroke()

                label = f'{w}×{h}'
                cr.set_dash([])
                cr.set_font_size(13)
                te = cr.text_extents(label)
                lx = x1 + 4
                ly = (y1 - 6) if y1 > 24 else (y2 + 18)
                cr.set_source_rgba(0, 0, 0, 0.75)
                cr.rectangle(lx - 3, ly - 14, te.width + 8, 18)
                cr.fill()
                cr.set_source_rgb(1, 1, 1)
                cr.move_to(lx, ly)
                cr.show_text(label)

    def _on_press(self, _widget, event):
        if event.button == 1:
            self._sx = self._cx = int(event.x)
            self._sy = self._cy = int(event.y)
            self._active = True

    def _on_motion(self, widget, event):
        if self._active:
            self._cx = int(event.x)
            self._cy = int(event.y)
            widget.queue_draw()

    def _on_release(self, _widget, event):
        if event.button == 1 and self._active:
            self._active = False
            x1 = min(self._sx, int(event.x));  y1 = min(self._sy, int(event.y))
            x2 = max(self._sx, int(event.x));  y2 = max(self._sy, int(event.y))
            if x2 - x1 > 4 and y2 - y1 > 4:
                self.result = self.screenshot.crop((x1, y1, x2, y2))
            Gtk.main_quit()

    def _on_key(self, _win, event):
        if event.keyval == Gdk.KEY_Escape:
            Gtk.main_quit()


# ── System tray ───────────────────────────────────────────────────────────────

def _make_tray_icon() -> tuple[str, str]:
    """
    Write the icon into a proper XDG theme directory structure so that
    AppIndicator can find it by name.  Returns (theme_dir, icon_name).
    """
    SIZE = 22
    theme_dir = os.path.join(tempfile.gettempdir(), 'nsnip-icons')
    apps_dir  = os.path.join(theme_dir, 'hicolor', '22x22', 'apps')
    os.makedirs(apps_dir, exist_ok=True)

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, SIZE, SIZE)
    cr = cairo.Context(surface)

    cr.set_source_rgba(0.18, 0.54, 0.92, 1.0)
    cr.arc(SIZE / 2, SIZE / 2, SIZE / 2, 0, 2 * math.pi)
    cr.fill()

    cr.set_source_rgba(1, 1, 1, 0.95)
    cr.set_line_width(1.8)
    cr.set_line_cap(cairo.LineCap.ROUND)
    m = SIZE / 2
    gap = 5
    cr.move_to(m, 2);       cr.line_to(m, m - gap)
    cr.move_to(m, m + gap); cr.line_to(m, SIZE - 2)
    cr.move_to(2, m);       cr.line_to(m - gap, m)
    cr.move_to(m + gap, m); cr.line_to(SIZE - 2, m)
    cr.stroke()
    cr.arc(m, m, gap - 1, 0, 2 * math.pi)
    cr.stroke()

    surface.write_to_png(os.path.join(apps_dir, 'nsnip.png'))
    return theme_dir, 'nsnip'


class TrayIcon:
    def __init__(self, on_snip, on_quit, cfg: dict, on_hotkey_change):
        self._cfg = cfg
        self._on_hotkey_change = on_hotkey_change
        menu = self._build_menu(on_snip, on_quit)
        theme_dir, icon_name = _make_tray_icon()

        if self._try_appindicator(theme_dir, icon_name, menu):
            return

        # Fallback: Gtk.StatusIcon (works on XFCE, KDE, MATE, etc.)
        icon_path = os.path.join(theme_dir, 'hicolor', '22x22', 'apps', 'nsnip.png')
        icon = Gtk.StatusIcon()
        icon.set_from_file(icon_path)
        icon.set_tooltip_text('NSnip — click to snip')
        icon.connect('activate', lambda _: GLib.idle_add(on_snip))
        icon.connect('popup-menu', self._status_icon_popup, menu)
        self._icon = icon  # keep alive

    def _try_appindicator(self, theme_dir: str, icon_name: str, menu: Gtk.Menu) -> bool:
        for ns in ('AyatanaAppIndicator3', 'AppIndicator3'):
            try:
                gi.require_version(ns, '0.1')
                from gi.repository import AyatanaAppIndicator3 as AI
                ind = AI.Indicator.new(
                    'nsnip', icon_name,
                    AI.IndicatorCategory.APPLICATION_STATUS
                )
                ind.set_icon_theme_path(theme_dir)
                # Menu must be set before ACTIVE or the library won't register
                ind.set_menu(menu)
                ind.set_status(AI.IndicatorStatus.ACTIVE)
                self._indicator = ind  # keep alive
                return True
            except Exception:
                continue
        return False

    def _build_menu(self, on_snip, on_quit) -> Gtk.Menu:
        menu = Gtk.Menu()

        item_snip = Gtk.MenuItem(label='Take Screenshot')
        item_snip.connect('activate', lambda _: GLib.idle_add(on_snip))
        menu.append(item_snip)

        menu.append(Gtk.SeparatorMenuItem())

        item_prefs = Gtk.MenuItem(label='Preferences…')
        item_prefs.connect('activate', lambda _: self._open_preferences())
        menu.append(item_prefs)

        item_update = Gtk.MenuItem(label='Check for Updates')
        item_update.connect('activate', lambda _: check_for_updates())
        menu.append(item_update)

        menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label='Quit')
        item_quit.connect('activate', lambda _: on_quit())
        menu.append(item_quit)

        menu.show_all()
        return menu

    def _open_preferences(self):
        PreferencesDialog(self._cfg, self._on_hotkey_change)

    def _status_icon_popup(self, icon, button, time, menu):
        menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, time)


# ── Snip action ───────────────────────────────────────────────────────────────

def snip() -> bool:
    """Capture → select region → annotate → copy. Returns GLib.SOURCE_REMOVE."""
    screenshot, bbox = capture_all_screens()

    # Phase 1: region selection
    sel_win = SnipWindow(screenshot, bbox)
    sel_win.show_all()
    sel_win.present()
    Gtk.main()
    selection = sel_win.result
    sel_win.destroy()

    if selection is None:
        return GLib.SOURCE_REMOVE

    # Phase 2: annotation
    ann_win = AnnotationWindow(selection)
    ann_win.connect('destroy', Gtk.main_quit)
    ann_win.show_all()
    Gtk.main()
    result = ann_win.result

    if result:
        copy_to_clipboard(result)
        w, h = result.size
        subprocess.Popen(
            ['notify-send', '-i', 'edit-copy', '-t', '2000', 'NSnip',
             f'Copied {w}×{h}px to clipboard'],
            stderr=subprocess.DEVNULL,
        )

    return GLib.SOURCE_REMOVE


# ── Daemon ────────────────────────────────────────────────────────────────────

def run_daemon():
    if WAYLAND:
        print(
            'Wayland detected: global hotkeys are not supported.\n'
            'Bind your DE shortcut to:  nsnip --once'
        )
        return

    try:
        from pynput import keyboard
    except ImportError:
        sys.exit('pynput not found. Run: pip install pynput')

    cfg = _load_config()
    loop = GLib.MainLoop()
    listener_box: list = [None]

    def start_listener(hotkey: str):
        if listener_box[0] is not None:
            listener_box[0].stop()
        listener_box[0] = keyboard.GlobalHotKeys(
            {hotkey: lambda: GLib.idle_add(snip)}
        )
        listener_box[0].start()

    def on_hotkey_change(new_hotkey: str):
        cfg['hotkey'] = new_hotkey
        start_listener(new_hotkey)
        print(f'NSnip shortcut updated: {_pynput_to_label(new_hotkey)}')

    def on_quit():
        if listener_box[0]:
            listener_box[0].stop()
        loop.quit()

    start_listener(cfg['hotkey'])

    tray = TrayIcon(
        on_snip=snip,
        on_quit=on_quit,
        cfg=cfg,
        on_hotkey_change=on_hotkey_change,
    )

    print(f'NSnip running. Shortcut: {_pynput_to_label(cfg["hotkey"])}')
    try:
        loop.run()
    except KeyboardInterrupt:
        if listener_box[0]:
            listener_box[0].stop()


# ── Setup ─────────────────────────────────────────────────────────────────────

def setup():
    """nsnip-setup: install icon and .desktop launcher."""
    import shutil

    theme_dir, _ = _make_tray_icon()
    src = os.path.join(theme_dir, 'hicolor', '22x22', 'apps', 'nsnip.png')

    icon_dir = os.path.expanduser('~/.local/share/icons/hicolor/22x22/apps')
    os.makedirs(icon_dir, exist_ok=True)
    dst = os.path.join(icon_dir, 'nsnip.png')
    shutil.copy2(src, dst)

    subprocess.run(
        ['gtk-update-icon-cache', '-f', '-t',
         os.path.expanduser('~/.local/share/icons/hicolor')],
        stderr=subprocess.DEVNULL,
    )

    apps_dir = os.path.expanduser('~/.local/share/applications')
    os.makedirs(apps_dir, exist_ok=True)
    nsnip_bin = shutil.which('nsnip') or 'nsnip'
    desktop = (
        '[Desktop Entry]\n'
        'Name=NSnip\n'
        'Comment=Freeze-first screenshot and annotation tool\n'
        f'Exec={nsnip_bin}\n'
        'Icon=nsnip\n'
        'Type=Application\n'
        'Categories=Graphics;Utility;\n'
        'StartupNotify=false\n'
    )
    desktop_path = os.path.join(apps_dir, 'nsnip.desktop')
    with open(desktop_path, 'w') as f:
        f.write(desktop)

    print(f'Icon  → {dst}')
    print(f'Entry → {desktop_path}')
    print('Done. Launch NSnip from your app menu or run: nsnip')


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if '--once' in sys.argv or '-o' in sys.argv:
        snip()
    else:
        run_daemon()


if __name__ == '__main__':
    main()
