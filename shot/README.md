# shot

Area screenshots, OCR, and a quick annotate-and-copy editor. A Shottr replacement.

| hotkey | does |
|---|---|
| **Opt+1** | drag an area → PNG on the clipboard (and a file, if `save_dir` is set) |
| **Opt+2** | drag an area → the text in it on the clipboard (Apple Vision OCR, on-device) |
| **Opt+`** | drag an area → editor window |

While selecting: **Esc** or right-click cancels. The selection shows true pixels inside the
frame and the size in points below it. Releasing with a selection smaller than 2×2 cancels.

## Editor

Opens at the spot you selected, with a toolbar across the top. Click a tool or press its
key:

| key | tool | | key | action |
|---|---|---|---|---|
| **A** | arrow (default) | | **Cmd+C** | copy annotated image and close |
| **R** | box | | **Cmd+S** | save PNG to `save_dir` (or ~/Desktop) and close |
| **P** | pen (free draw) | | **Cmd+Z** | undo last item |
| **T** | text: click, type, Return | | **Esc** / **Cmd+W** | close without copying |
| **B** | pixelate a region | | | |

The editor owns the keyboard until you click somewhere else; click back into it to
continue. Capture hotkeys still work while it is open (Opt+1 closes it and starts a new
capture).

## Why it is fast, and why the screen freezes

On the hotkey the display under the cursor is captured with `CGWindowListCreateImage`
(~1 ms warm; the ~40 ms pipeline setup is paid once at startup) and that frame becomes
the content of a resident full-screen panel. Everything you see while selecting is the
frozen frame: a video keeps playing underneath, a caret keeps blinking, a notification
can slide in — none of it is on screen and none of it can end up in the shot. The
selection is cut from the same frame, so what you copy is exactly what was there at
keypress, cursor and dimming excluded.

Measured on an M-series Mac at 2x: hotkey → frozen frame on screen 15–19 ms (one frame;
the ~12 ms is Core Animation committing a 4112×2658 texture, 45 ms after a long idle);
mouse-up → PNG on the clipboard ~11 ms; OCR of a text-heavy 1200×700 region ~450 ms (the
Vision model is warmed at startup).

## Config — `~/.config/mactools/shot.json`

```json
{
  "area_to_clipboard": "opt+1",
  "ocr_to_clipboard": "opt+2",
  "area_to_editor": "opt+`",
  "save_dir": "",
  "ocr_languages": ["en-US"],
  "ocr_auto_language": false,
  "editor_color": "#ff3b30",
  "editor_stroke": 4,
  "debug": false
}
```

`save_dir` (e.g. `"~/Desktop"`) makes Opt+1 also write a timestamped PNG. `ocr_languages`
is a priority list of Vision language codes; `ocr_auto_language` lets Vision guess instead
(worse on short snippets: a lone × can flip the whole result to another script). Any
hotkey may be `""` to disable it.

## Install

```sh
./install.sh            # install / restart
./install.sh --uninstall
```

Permissions for `/usr/bin/python3`: **Accessibility** (the hotkey tap) and **Screen
Recording** (System Settings → Privacy & Security → Screen & System Audio Recording).
Without Screen Recording the captures contain only the wallpaper; the daemon logs this
at start. See the top-level README for the ⌘⇧G trick.

## Notes

- `CGWindowListCreateImage` is deprecated since macOS 14 in favour of ScreenCaptureKit but
  present and fast on 26.x; ScreenCaptureKit is async/Swift-shaped and not reachable from
  ctypes without a helper binary. If Apple removes it this package needs that helper.
- One private call: `SLSSetConnectionProperty(..., "SetsCursorInBackground")` so the
  crosshair cursor shows while a background process owns the selection; the cursor is
  re-asserted on every mouse move because the frontmost app resets it on each one it
  receives. Without the property the arrow cursor stays; everything else works.
- Multi-display: the overlay covers the display under the cursor at hotkey time, at that
  display's own backing scale.
