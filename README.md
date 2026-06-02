# NSnip

Freeze-first screenshot tool for Linux with region selection, annotations, and system tray integration.

## Features

- **Freeze-first** — captures all screens before you interact, so nothing changes while you aim
- **Region selection** — drag to choose exactly what to copy
- **Annotations** — pen, rectangle, arrow, and text overlays
- **System tray** — runs in the background; trigger with Print Screen
- **`--once` mode** — single-shot capture for DE keyboard shortcuts (Wayland-friendly)

## Install

### 1. Install system dependencies

```bash
sudo apt install python3-gi python3-gi-cairo \
                 gir1.2-gtk-3.0 gir1.2-gdk-3.0 \
                 gir1.2-gio-2.0 gir1.2-gdkpixbuf-2.0
```

### 2. Install the package

```bash
pip3 install --user --break-system-packages git+https://github.com/NoeGarCou/nsnip.git
```

### 3. Set up the app icon and launcher entry

```bash
nsnip-setup
```

## Usage

**Daemon mode** — runs in the background, binds Print Screen globally (X11 only):

```bash
nsnip
```

**Single-shot mode** — one capture then exits; works on Wayland and with DE shortcuts:

```bash
nsnip --once
```

On Wayland, bind `nsnip --once` to a keyboard shortcut in your desktop environment settings.

## Workflow

1. Press **Print Screen** (or run `nsnip --once`)
2. **Drag** to select the region you want
3. **Annotate** with pen, rectangle, arrow, or text
4. Click **Copy & Close** — the result lands on your clipboard

## Keyboard shortcuts (annotation window)

| Action | Shortcut |
|--------|----------|
| Confirm & copy | Ctrl+Enter |
| Undo last annotation | Ctrl+Z |
| Cancel | Escape |
