# NSnip

A Windows 11 style Snipping Tool for Linux.

Press **Super+Shift+S**: the screen freezes, you pick what to capture, and the snip
is on your clipboard. Click the notification to mark it up.

## Features

- **Freeze-first**: every monitor is captured before you aim, so nothing moves under you
- **Four snipping modes**: Rectangle, Freeform, Window and Full screen, from a toolbar at the top of the screen
- **Instant clipboard**: the snip is copied the moment you release the mouse
- **Auto-save**: snips are also saved to `~/Pictures/Screenshots` (can be turned off)
- **Mark-up editor**: ballpoint pen, highlighter, eraser, shapes (rectangle, oval, line, arrow), text, crop, undo/redo and zoom
- **Edits copy themselves**: changes in the editor go to the clipboard as you make them
- **Multi-monitor and mixed layouts** work as expected
- **Delayed snips**: 3, 5 or 10 seconds, from the tray or the editor

## Install

### 1. Install system dependencies

```bash
sudo apt install python3-gi python3-gi-cairo \
                 gir1.2-gtk-3.0 gir1.2-gdk-3.0 \
                 gir1.2-gio-2.0 gir1.2-gdkpixbuf-2.0 \
                 gir1.2-wnck-3.0 gir1.2-ayatanaappindicator3-0.1
```

`gir1.2-wnck-3.0` enables Window mode. `gir1.2-ayatanaappindicator3-0.1` gives a modern tray icon.

### 2. Install the package

```bash
pip3 install --user --break-system-packages git+https://github.com/NoeGarCou/nsnip.git
```

### 3. Set it up

```bash
nsnip-setup
```

This installs the icon and launcher, starts NSnip at login, and registers the
**Super+Shift+S** shortcut with Cinnamon or GNOME.

## Usage

```bash
nsnip                 # run in the tray (started at login after nsnip-setup)
nsnip --snip          # take a snip now; instant when NSnip is already running
nsnip --snip --mode window --delay 3
nsnip --edit image.png
```

On Cinnamon and GNOME the shortcut is a regular desktop keybinding, managed from
NSnip's **Settings**. There you can also let NSnip take over the **Print Screen** key
(Cinnamon). On other desktops, bind `nsnip --snip` yourself, or keep NSnip running
in the tray (X11) to use its built-in shortcut.

## Workflow

1. Press **Super+Shift+S** (or click the tray icon)
2. Pick a mode in the toolbar, then drag a rectangle or a freeform shape, or click a window or screen
3. The snip is copied to the clipboard and a notification appears
4. Click the notification to open the editor, mark up, then paste anywhere

## Editor shortcuts

| Action | Shortcut |
|--------|----------|
| New snip | Ctrl+N |
| Copy | Ctrl+C |
| Save as | Ctrl+S |
| Open image | Ctrl+O |
| Undo / Redo | Ctrl+Z / Ctrl+Y |
| Zoom | Ctrl+scroll, Ctrl+plus / Ctrl+minus |
| Actual size / Fit | Ctrl+1 / Ctrl+0 |
| Finish text or crop | Enter |
| Cancel text or crop | Esc |

Click an active pen, highlighter, shape or text button again to change its colour and size.

## Snipping overlay

| Action | Shortcut |
|--------|----------|
| Cancel | Esc |
